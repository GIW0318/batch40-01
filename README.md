# GSB Bugfix Task
## 题面
凌晨两点，清洗设备告警说边缘集群的 QUIC 握手被拿来放大流量，值班同学把抓包和连接表翻了一遍，发现防线几乎每个环节都漏。Retry 票据在密钥换了之后还能验过，换一个源端口也照收；握手完成前发出的字节数没有和收到字节做比例限制；客户端换地址后只更新了连接五元组，路径挑战没有重做；0-RTT 数据在窗口外仍被投递；无状态重置包里能读出固定的 CID 前缀；票据过期用的是各节点自己的墙上时钟；NAT 重绑定被误判成迁移；并发握手各自记预算，同一地址能突破总限额；票据长度异常时解析器继续向后读；日志只写拒绝，不写原因。quic_retry_amplification.py 的 QuicRetryGuard 要管 check_initial、issue_retry、validate_token、on_migration 和 snapshot。票据必须绑定地址和初始包，反放大额度按连接原子扣减，迁移重新做路径验证，0-RTT 重放窗口严格生效，过期只认单调时钟，异常输入不改变状态，统计按原因拆开。tests/test_quic_retry_amplification.py 里的十五条检查目前一条都过不了，覆盖票据轮换、端口变化、并发预算、迁移挑战、0-RTT 重放、前缀泄露、时钟漂移、NAT 重绑、长度异常和状态不变。

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
