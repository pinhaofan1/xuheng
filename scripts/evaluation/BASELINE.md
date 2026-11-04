# 固定评测基线

`baseline_conflict_service.txt` 是上游文件的原始字节副本，供没有 `.git` 的源码压缩包复跑对照；不是本次新写的代码。

- 来源：https://github.com/shenminglinyi/PlotPilot
- 提交：`8dba88fad06368a632522ce6f98e17f9cfc7db2a`
- 路径：`application/audit/services/conflict_detection_service.py`
- SHA-256：`e46ae870eeb68e3b8a9b94aae8537f96a6c258544523b09c88e2cd5e9f04f93d`
- 版权与许可：保留上游作者和贡献者权利，适用根目录完整 `LICENSE`。

评测器优先读取 Git 固定提交，缺少该对象时读取副本；两种路径均验证同一校验和。
