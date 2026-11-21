# GSB Bugfix Task
## 题面
服务网格切 mTLS 后，审计发现跨命名空间调用没被真正拦住。攻击者拿一个合法工作负载证书，就能冒充别的服务：SPIFFE ID 只比较了前缀，trust domain 不同也放行；证书 SAN 里有多个 URI 时只取第一个；证书轮换期间旧证书还能继续建连；吊销列表没生效，被撤销的证书仍被接受；连接池按地址复用，不按对端身份隔离，A 服务的连接被 B 服务复用；mTLS 降级到明文时只记日志；JWT 与证书身份不一致时信了 JWT；授权策略只匹配路径不匹配方法；同名服务在不同命名空间被视为同一主体；sidecar 重启后策略缓存没有失效；审计只记允许不记拒绝；证书链里中间 CA 没有校验 basicConstraints。grpc_mtls_identity_fence.py 的 MtlsIdentityFence 负责 check_peer、authorize、bind_connection、on_rotation 和 state_dump。对端身份要从证书链和 SPIFFE ID 双重校验；trust domain、命名空间和服务名必须逐段匹配；SAN 多值全部校验；轮换和吊销实时生效；连接池按身份隔离；禁止静默降级；JWT 与证书冲突时拒绝；策略同时匹配方法、路径和身份；跨命名空间默认拒绝；缓存随轮换失效；拒绝也写审计。test_grpc_mtls_identity_fence.py 的十五个断言眼下全红，涉及 SPIFFE 前缀、trust domain、SAN 多值、轮换、吊销、连接复用、降级、JWT 冲突、方法匹配、命名空间、缓存和审计。

## 运行方式
```bash
python -m pytest tests -q
```

## 平台字段

所属领域：安全
子方向：身份认证
任务类型：Bugfix
语言 / 框架：Python 3, pytest
环境可复现等级：有外部依赖，未容器化
