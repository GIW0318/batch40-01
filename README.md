# GSB Bugfix Task
## 题面
递归解析器接入 DNSSEC 后，安全运营发现一些明明验签失败的域名被当成不安全放行，另一些正常域名却被判成伪造。现场能复现的情况包括：缺少 DNSKEY 时直接降级成 insecure；NSEC3 迭代次数超过上限仍继续算；通配符否定证明只校验了最左标签；opt-out 区间被当成完整证明；否定缓存的 TTL 取了 SOA 最小值而不是证明里的值；bogus 结果被写进普通缓存，下一次直接返回；信任锚轮换期间新旧密钥同时存在，代码只认第一把；CD 和 AD 位在转发时被覆盖；UDP 截断后没有回退 TCP 就下结论；CNAME 链中间某一跳没有验签；NSEC 覆盖区间计算错一位；缓存命中时跳过了过期检查。dnssec_negative_trust.py 的 NegativeTrustAnchor 负责 validate、negative_proof、cache、trust_anchor 和 resolve，验证结果、缓存和信任锚现在没有统一状态。要求严格区分 bogus、insecure 和 secure，否定证明必须覆盖完整区间，opt-out 和通配符分别处理，否定 TTL 按证明取最小，bogus 不进普通缓存，轮换期多锚并存，CD/AD 位按策略保留，截断回退 TCP，CNAME 逐跳验证。test_dnssec_negative_trust.py 的十五条断言现在全部失败，覆盖缺钥降级、NSEC3 迭代、通配符、opt-out、否定 TTL、bogus 缓存、锚轮换、标志位、截断回退、CNAME 链、区间边界和缓存过期。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：网络与通信安全
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
