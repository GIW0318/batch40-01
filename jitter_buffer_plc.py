SEQ_MOD = 1 << 16
SEQ_HALF = 1 << 15


class JitterBuffer:
    def __init__(self, frame_ms=20, max_delay_ms=200, sample_rate=48000,
                 capacity_packets=None):
        self.frame_ms = frame_ms
        self.max_delay_ms = max_delay_ms
        self.sample_rate = sample_rate
        self.max_packets = capacity_packets

        self.target_delay_ms = max_delay_ms
        self.vad_threshold = 0.01

        self._packets = {}
        self._playout_seq = None

        self._stats = {
            "pushed": 0,
            "played": 0,
            "concealed": 0,
            "duplicates": 0,
            "fec_duplicates": 0,
            "late": 0,
            "overflow": 0,
            "drift_frames": 0,
            "dropped_frames": 0,
            "resampled": 0,
        }

    def frame_samples(self):
        return self.sample_rate * self.frame_ms // 1000

    def capacity_packets(self):
        if self.max_packets is not None:
            return self.max_packets
        return max(1, self.max_delay_ms // self.frame_ms)

    def _signed_diff(self, first, second):
        diff = (first - second) % SEQ_MOD
        if diff >= SEQ_HALF:
            diff -= SEQ_MOD
        return diff

    def sequence_newer(self, first, second):
        return self._signed_diff(first, second) > 0

    def _is_late(self, seq):
        if self._playout_seq is None:
            return False
        return self._signed_diff(seq, self._playout_seq) < 0

    def _oldest_seq(self):
        base = self._playout_seq
        if base is None:
            base = next(iter(self._packets))
        return min(self._packets, key=lambda seq: self._signed_diff(seq, base))

    def resample(self, samples, from_rate, to_rate):
        if from_rate == to_rate or not samples:
            return list(samples)
        out_len = max(1, round(len(samples) * to_rate / from_rate))
        last_index = len(samples) - 1
        resampled = []
        for index in range(out_len):
            position = index * from_rate / to_rate
            left = int(position)
            fraction = position - left
            if left >= last_index:
                resampled.append(samples[last_index])
            else:
                resampled.append(
                    samples[left] * (1.0 - fraction)
                    + samples[left + 1] * fraction
                )
        return resampled

    def _align_frame_length(self, samples):
        target = self.frame_samples()
        if len(samples) == target:
            return samples
        self._stats["drift_frames"] += 1
        if len(samples) > target:
            return samples[:target]
        padding = samples[-1:] if samples else [0.0]
        return samples + padding * (target - len(samples))

    def push(self, seq, samples, sample_rate=None, fec=False):
        seq %= SEQ_MOD
        samples = list(samples)

        if self._is_late(seq):
            self._stats["late"] += 1
            return False

        if seq in self._packets:
            if fec or self._packets[seq]["fec"]:
                self._stats["fec_duplicates"] += 1
            else:
                self._stats["duplicates"] += 1
            return False

        source_rate = sample_rate or self.sample_rate
        if source_rate != self.sample_rate:
            samples = self.resample(samples, source_rate, self.sample_rate)
            self._stats["resampled"] += 1

        samples = self._align_frame_length(samples)

        while len(self._packets) >= self.capacity_packets():
            oldest = self._oldest_seq()
            del self._packets[oldest]
            self._stats["overflow"] += 1
            if oldest == self._playout_seq:
                self._playout_seq = (self._playout_seq + 1) % SEQ_MOD

        self._packets[seq] = {
            "seq": seq,
            "samples": samples,
            "length": len(samples),
            "concealed": False,
            "fec": fec,
            "sample_rate": self.sample_rate,
        }
        self._stats["pushed"] += 1
        return True

    def _concealed_frame(self, seq):
        self._stats["concealed"] += 1
        length = self.frame_samples()
        return {
            "seq": seq,
            "samples": [0.0] * length,
            "length": length,
            "concealed": True,
            "fec": False,
            "sample_rate": self.sample_rate,
        }

    def pop(self):
        if self._playout_seq is None:
            if self._packets:
                self._playout_seq = self._oldest_seq()
            else:
                self._playout_seq = 0

        seq = self._playout_seq
        self._playout_seq = (self._playout_seq + 1) % SEQ_MOD

        frame = self._packets.pop(seq, None)
        if frame is not None:
            self._stats["played"] += 1
            return frame
        return self._concealed_frame(seq)

    def conceal(self, frames=1):
        frame = None
        for _ in range(max(1, frames)):
            if self._playout_seq is None:
                self._playout_seq = 0
            seq = self._playout_seq
            self._playout_seq = (self._playout_seq + 1) % SEQ_MOD
            frame = self._concealed_frame(seq)
        return frame

    def set_target_delay(self, ms):
        clamped = max(self.frame_ms, min(self.max_delay_ms, ms))
        self.target_delay_ms = clamped
        return clamped

    def adapt_delay(self, loss_rate, jitter_ms):
        target = (
            self.frame_ms
            + jitter_ms
            + loss_rate * self.max_delay_ms * 3
        )
        return self.set_target_delay(target)

    def set_vad_threshold(self, threshold):
        self.vad_threshold = threshold

    def is_voice_active(self, rms):
        return rms >= self.vad_threshold

    def stats(self):
        result = dict(self._stats)
        result["target_delay_ms"] = self.target_delay_ms
        result["buffered_packets"] = len(self._packets)
        result["buffered_ms"] = len(self._packets) * self.frame_ms
        return result
