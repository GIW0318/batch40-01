class RuleShadowAnalyzer:
    def __init__(self, default_policy="deny"):
        self.default_policy = default_policy

    def add_rule(self, rule):
        raise NotImplementedError()

    def normalize(self, rule):
        raise NotImplementedError()

    def analyze(self):
        raise NotImplementedError()
