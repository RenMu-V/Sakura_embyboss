# 这个 fork 改了什么（HERMES-FORK）

上游：[berry8838/Sakura_embyboss](https://github.com/berry8838/Sakura_embyboss)

本 fork 只做「修 bug」这一件事，全部改动都在下面列清楚，方便以后跟上游合并。
背景是一次真实故障：bot 容器 `Up`、健康检查全绿、`RestartCount=0`，但 **16 小时**
零响应（5 个群的一条通知都发不出去）；另有一次容器重建后 **所有发群操作全部失败**。

## 改动清单

### 1. 构建期修 pyrogram 2.0.106（`tools/patch_pyrogram.py` + `Dockerfile`）

`Dockerfile` 里加了一行 `RUN python3 tools/patch_pyrogram.py`，只改镜像内 site-packages
的第三方库，**不动 bot 自身源码**，因此不影响 bot 的 git 自动更新。补丁在构建期做
语法 + 标记 + 行为断言（含 `get_peer_type` 的真实输入输出），锚点找不到就让构建失败。

**A. 会话静默卡死的三个成因**（`pyrogram/session/session.py`）

| 位置 | 原实现 | 后果 |
| --- | --- | --- |
| `recv_worker` | 只在 `is_started` 已置位时才安排重连 | 连接在 `start()` 置位前就断 → 没人重连 |
| `ping_worker` | `except (OSError, RPCError): pass` | ping 每 5 秒都失败也不触发重连（唯一探测点被废掉）|
| `start()` | 非 OSError 异常 `raise` 出去 | 异常死在 `restart()` 这个 task 里，此后再无人重连 |

另有 `invoke` 重试 10 次耗尽后只抛错、不修会话。

改法：ping 连续失败 3 次 → 重建会话；`invoke` 遇到 `handler is closed` → 主动重建；
`start()` 失败改为退避重试不再抛死；`restart()` 加单飞保护（避免并发重建把 Session
撕成两份连接）。

**B. peer 解析：冷启动解析不了群/频道**

- `utils.get_peer_type` 的 `MIN_CHANNEL_ID = -1002147483647` 只覆盖 `channel_id < 2^31`。
  Telegram 的群/频道 id 早已超过 2^31（实测 `channel_id = 2257538689`），这类 peer 会被
  直接判 `Peer id invalid`，`resolve_peer` 连分支都进不去。
  → 放宽到 `-1999999999999`（`peer_id = MAX_CHANNEL_ID - channel_id`，覆盖 `channel_id < 1e12`）。
- `resolve_peer` 的 chat/channel 分支只 `await self.invoke(...)` **丢掉返回值**、
  没有 `fetch_peers`，所以后面 `storage.get_peer_by_id()` 依然 KeyError。
  （user 分支本来就是对的，照抄即可。）→ 两个分支都补 `fetch_peers`。
- `Client` 的 `workdir` 默认 `"."`（容器内 `/app`，临时层）：容器重建就丢 auth key 与
  peer 缓存。→ 支持用 `BOT_SESSION_DIR` 指定持久化目录。

> 这三条合起来才是「容器重建后发不出消息」的完整原因，且**潜伏期很长**：只有 session
> 是新的（peers 表没记录）时才暴露，老 session 因为缓存命中反而正常。

**A/B 验证**（同一个冷启动脚本，只调 `get_chat(群id)`）：

```
官方镜像   -> 失败: ValueError: Peer id invalid: -1002257538689
本 fork 镜像 -> 成功: -1002257538689 (酥酥糖)
```

### 2. bot 自身的小改动（可单独提 PR 给上游）

- `bot/__init__.py`：`Client(..., workdir=os.environ.get("BOT_SESSION_DIR", "."))`，
  让 session 能挂持久化卷。
- `bot/func_helper/msg_utils.py`：`warmup_peer_cache()` 的调用点是
  `loop.call_later(8, ...)`，冷启动 8 秒可能还没连上，原实现直接失败并放弃；改为先等
  `bot.is_connected`（最多 60 秒）再解析。

## 部署

```bash
# 用 .env 或 compose 指定 session 目录（挂到持久化卷）
BOT_SESSION_DIR=/app/session   # compose: volumes: ./session:/app/session

docker compose build embyboss
docker compose up -d --no-deps embyboss
```

`/app/session/<bot>.session` 落盘后，容器 rebuild / recreate **不再重新登录**——
这很关键：反复重新登录会触发 Telegram 限流
（`[420 FLOOD_WAIT_X] ... auth.ImportBotAuthorization`，实测一次 2645 秒），
每次都要等窗口过去，期间容器会陷入「启动 → 登录被拒 → 退出 → 再启动」的循环。

## 跟上游合并

```bash
git fetch upstream
git merge upstream/master     # 或 git rebase upstream/master
# 冲突通常只可能在 bot/__init__.py 与 bot/func_helper/msg_utils.py 这两处小改动
docker compose build embyboss && docker compose up -d --no-deps embyboss
```

`tools/patch_pyrogram.py` 的锚点如果在上游/新版 pyrogram 里变了，构建会**直接失败**并
打印是哪个锚点找不到——这正是期望行为，改锚点即可，不要去掉断言。
