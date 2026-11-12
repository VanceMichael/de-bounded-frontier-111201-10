# 授权网站·有界链接发现

面向合作资料接收人员的 Python 采集组件。从 HTML 提取相对链接与文档基地址，提供单地址 robots 判定。
输入样本都是本地合成数据，example 域名只表示来源身份，不需联网访问。

## 安装与运行

使用 Python 3.11，依赖仅为标准库，SQLite 随解释器提供，无需安装第三方包。
从项目根目录执行；演示启动真实的回环 HTTP 来源、处理响应、事务入库后读取实际结果。
每次演示使用独立临时目录，结束后清理；通常两秒内结束，不回放历史输出。

```bash
python3 -m compileall -q collector
python3 -m unittest discover -s tests -v
python3 -m collector demo
python3 -m collector crawl-demo
```

常规导入保留在本地数据库中，可重复运行；默认路径已忽略，不会进入版本库。

```bash
python3 -m collector import --input fixtures/sample.json --db var/records.sqlite3
python3 -m collector show --db var/records.sqlite3
python3 -m collector serve --db var/records.sqlite3 --port 8080
```

查询服务监听回环地址，`GET /records` 返回数据库中的记录；按 Ctrl-C 停止。

## 有界发现前沿

`crawl` 在许可边界内推进可持久化的发现前沿，`frontier` 查询各地址状态与理由：

```bash
python3 -m collector crawl --license fixtures/access.json --db var/frontier.sqlite3 --limit 1
python3 -m collector frontier --db var/frontier.sqlite3 --state denied
python3 -m collector crawl --license fixtures/access.json --db var/frontier.sqlite3 --license-version 2
```

许可文件（`fixtures/access.json`）给出起始地址、允许的 origin 与路径前缀、
跟踪参数与语义参数、最大深度与总请求预算，以及版本化更新（撤销路径）。
行为要点：

- 归一化只移除许可列出的跟踪参数，筛选条件等语义参数一律保留；
  锚点、默认端口与参数顺序视为等价，其余差异绝不归并。
- 每一跳（含 302 跳转目标）都重新检查许可与 robots；起点合法不意味着可以一路跟随。
  robots 暂不可用时按暂停处理，绝不假定允许；拒绝地址绝不发出请求。
- 许可升级后，仍在队列但不再授权的地址转为拒绝并记录理由，不会执行。
- 调度按来源公平分配：已访问份额最少的来源优先，单个高分支页面不会吃光其他栏目额度。
- 预算按实际请求数（页面与 robots）严格执行；耗尽时剩余队列标记为预算耗尽。
- 前沿状态有 queued / visited / denied / paused / exhausted 五种，
  每条记录带有可查询理由（如 license_revoked、robots_unavailable、budget_exhausted），
  暂停与预算耗尽的地址在下次运行时自动回到队列，重启后可继续合法前沿。

`crawl-demo` 在本地镜像上演示完整流程：v1 许可限量运行、重启后按 v2 许可继续，
并输出镜像收到的全部请求作为未越界的证明。`--mirror` 可把许可 origin 映射到本地镜像地址。

## 组件与数据

`collector/domain.py` 负责来源格式和领域校验；`storage.py` 保留原始对象、导入凭据和当前记录。
单次导入在同一事务内提交，异常时整体回滚；相同来源和原文只登记一次凭据。
当前记录以领域标识为键，同键再次导入替换当前观察值，不代表保留了完整修订历史。
`transport.py` 提供请求客户端、本地响应服务、许可 origin 镜像映射与站点地图镜像构造；
响应服务按路径依次消费响应脚本，并记录实际请求。
`frontier.py` 提供许可解析、持久化发现前沿与有界爬取。`api.py` 提供只读查询入口。
`fixtures/sample.json` 是当前导入器支持的输入；`fixtures/access.json` 与
`fixtures/site-map.json` 描述许可与合成站点。其他附件描述同领域的合成页面、协议或事件，
可以由调用方构造本地来源。
当前响应服务是顺序脚本工具，不自动推断认证、文件或表单业务；领域对象提供对应的本地交互。

## 可复现运行

宿主机或容器均不需要网络。镜像准备好以后可以关闭外部网络再运行上述命令。
仓库内不含真实账号、新闻正文或人员资料；不要将生产凭据写入样本或日志。
