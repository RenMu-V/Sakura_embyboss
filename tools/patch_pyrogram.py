#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建期补丁: 修 pyrogram 2.0.106 的三个真实缺陷。

本文件在 Dockerfile 构建阶段执行(RUN python3 tools/patch_pyrogram.py), 只改镜像里
site-packages 的第三方库, 不改 bot 自身源码, 因此不影响 bot 的 git 自动更新。

=== A. session/session.py: 会话会静默卡死(实测失联 16 小时) ===
现象: 容器 Up、FastAPI 正常、RestartCount=0, 但每次 Telegram 调用都直接失败:
      OSError: ... <TCPTransport closed=True ...>; the handler is closed
读源码得到三个各自独立的成因:
  A1 recv_worker 只在 is_started 已置位时才安排重连。若连接在 start() 的
     is_started.set() 之前就断, 就没人重连 —— 会话停在"已 started、transport 已关、
     没有 recv 任务"的状态, 唯一还可能发现它的就是 ping。
  A2 ping_worker 里 `except (OSError, RPCError): pass` 把失败全吞掉: 每 5 秒一次的 ping
     一直失败也不会触发重连, 于是 A1 那种状态就永远没人管。
  A3 start() 里 `except Exception: await self.stop(); raise e` —— 非 OSError 异常从
     restart() 这个 task 里静默抛出, 此后再没人重连; 且 OSError 分支的重试没有退避。
  A4 Session.invoke 重试 10 次耗尽后只抛错, 不会去修会话。
修补: ping 连续失败触发重建; transport 关闭时 invoke 主动重建; start() 退避重试不再抛死;
      restart() 加单飞保护。

=== B. peer 解析: 冷启动解析不了群/频道 ===
  B1 utils.get_peer_type 的 MIN_CHANNEL_ID = -1002147483647 只覆盖 channel_id < 2^31。
     Telegram 的群/频道 id 早已超过 2^31(实测某群 channel_id=2257538689), 这类 peer 会被
     直接判 "Peer id invalid", resolve_peer 连分支都进不去。
     这种 bug 潜伏期很长: 只有 session 是"新的"(peers 表里没记录)时才暴露, 一旦容器
     被 recreate 就立刻发病 —— 表现为 peer 预热失败 + 日榜/通知等所有发群操作全挂。
  B2 resolve_peer 的 chat/channel 分支只 `await self.invoke(...)` 丢掉返回值, 没有
     fetch_peers, 所以后面 storage.get_peer_by_id() 依然 KeyError -> PeerIdInvalid。
     (user 分支是对的: `fetch_peers(await invoke(users.GetUsers(...)))`。)
  B4 Client 的 workdir 默认 "."(容器内 /app, 临时层): 容器 recreate 就丢 auth key 与
     peer 缓存, 每次都要重新登录 —— 反复登录还会触发 Telegram 登录限流
     ([420 FLOOD_WAIT_X] ... auth.ImportBotAuthorization)。改为可用 BOT_SESSION_DIR 指定
     持久化目录。
"""
import io
import os
import sys

import pyrogram

# 顺着已安装的 pyrogram 定位 site-packages, 不写死路径
SITE = os.path.dirname(os.path.dirname(os.path.abspath(pyrogram.__file__)))
SESSION = os.path.join(SITE, "session", "session.py")
RESOLVE = os.path.join(SITE, "methods", "advanced", "resolve_peer.py")
UTILS = os.path.join(SITE, "utils.py")
CLIENT = os.path.join(SITE, "client.py")


def patch(path, old, new, tag):
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    if new in src:
        print("  [skip] %s (已打过)" % tag)
        return
    if old not in src:
        raise SystemExit("!! 补丁锚点找不到: %s (%s)" % (tag, path))
    if src.count(old) != 1:
        raise SystemExit("!! 锚点不唯一(%d 处): %s" % (src.count(old), tag))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new))
    print("  [ok] %s" % tag)


def append_once(path, code, marker, tag):
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    if marker in src:
        print("  [skip] %s (已打过)" % tag)
        return
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(code)
    print("  [ok] %s" % tag)


def main():
    print("== A. session/session.py ==")

    patch(
        SESSION,
        """            except (OSError, RPCError):
                await self.stop()
            except Exception as e:
                await self.stop()
                raise e
            else:
                break""",
        """            except (OSError, RPCError):
                await self.stop()
                await asyncio.sleep(3)
            except Exception as e:
                # [patch] 原来这里 raise: 异常从 restart() 这个 task 里静默抛出,
                # 之后没有任何东西会重连, 会话永久停在 "transport closed"。
                log.warning("Session start failed (%s), retry in 3s: %s",
                            type(e).__name__, e)
                await self.stop()
                await asyncio.sleep(3)
            else:
                break""",
        "A3 start() 失败不再抛死, 退避重试",
    )

    patch(
        SESSION,
        """    async def restart(self):
        await self.stop()
        await self.start()""",
        """    async def restart(self):
        # [patch] 单飞: 避免多路失败同时重建, 把 Session 撕成两份连接
        if getattr(self, "_restarting_guard", False):
            log.warning("Session 重建已在进行中, 跳过重复请求")
            return
        self._restarting_guard = True
        try:
            await self.stop()
            await self.start()
        finally:
            self._restarting_guard = False""",
        "A1 restart() 单飞保护",
    )

    old_ping = '''    async def ping_worker(self):
        log.info("PingTask started")

        while True:
            try:
                await asyncio.wait_for(self.ping_task_event.wait(), self.PING_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            try:
                await self.send(
                    raw.functions.PingDelayDisconnect(
                        ping_id=0, disconnect_delay=self.WAIT_TIMEOUT + 10
                    ), False
                )
            except (OSError, RPCError):
                pass

        log.info("PingTask stopped")'''
    new_ping = '''    async def ping_worker(self):
        log.info("PingTask started")

        _fails = 0

        while True:
            try:
                await asyncio.wait_for(self.ping_task_event.wait(), self.PING_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            try:
                await self.send(
                    raw.functions.PingDelayDisconnect(
                        ping_id=0, disconnect_delay=self.WAIT_TIMEOUT + 10
                    ), False
                )
                _fails = 0
            except (OSError, RPCError) as e:
                # [patch] 原来这里只 pass: ping 一直失败也不重连。
                # 这是"已 started 但 transport 已关"这种卡死状态唯一的周期性探测点。
                _fails += 1
                log.warning("Ping failed (%s/%s): %s", _fails, 3, e)
                if _fails >= 3:
                    _fails = 0
                    if self.is_started.is_set():
                        log.warning("Ping 连续失败 -> 重建会话")
                        self.loop.create_task(self.restart())
                        break

        log.info("PingTask stopped")'''
    patch(SESSION, old_ping, new_ping, "A2 ping_worker 连续失败触发重建")

    patch(
        SESSION,
        """            except (OSError, InternalServerError, ServiceUnavailable) as e:
                if retries == 0:
                    raise e from None""",
        """            except (OSError, InternalServerError, ServiceUnavailable) as e:
                if retries == 0:
                    # [patch] 传输层已关时只抛错没有用, 顺手触发一次会话重建
                    if self.is_started.is_set() and "handler is closed" in str(e):
                        log.warning("传输层已关 -> 重建会话: %s", e)
                        self.loop.create_task(self.restart())
                    raise e from None""",
        "A4 invoke 失败触发重建",
    )

    print("== B. utils.py / resolve_peer.py / client.py ==")

    patch(
        UTILS,
        "MIN_CHANNEL_ID = -1002147483647",
        """# [patch] 原值 -1002147483647 只覆盖 channel_id < 2^31。Telegram 的群/频道 id 早已
# 超过 2^31(实测某群 channel_id=2257538689), 这类 peer 会被 get_peer_type 直接判
# "Peer id invalid", 冷启动的 session 就解析不了群 -> 预热/发群全部失败。
# peer_id = MAX_CHANNEL_ID - channel_id, 放宽到覆盖 channel_id < 1e12。
MIN_CHANNEL_ID = -1999999999999""",
        "B1 MIN_CHANNEL_ID 放宽(支持 > 2^31 的频道 id)",
    )

    patch(
        RESOLVE,
        """            elif peer_type == "chat":
                await self.invoke(
                    raw.functions.messages.GetChats(
                        id=[-peer_id]
                    )
                )
            else:
                await self.invoke(
                    raw.functions.channels.GetChannels(
                        id=[
                            raw.types.InputChannel(
                                channel_id=utils.get_channel_id(peer_id),
                                access_hash=0
                            )
                        ]
                    )
                )""",
        """            elif peer_type == "chat":
                # [patch] 原来只 invoke 不 fetch_peers: 响应被丢掉, 后面
                # get_peer_by_id 照样 KeyError -> PeerIdInvalid(冷启动解析不了群/频道)
                await self.fetch_peers(
                    (await self.invoke(
                        raw.functions.messages.GetChats(
                            id=[-peer_id]
                        )
                    )).chats
                )
            else:
                await self.fetch_peers(
                    (await self.invoke(
                        raw.functions.channels.GetChannels(
                            id=[
                                raw.types.InputChannel(
                                    channel_id=utils.get_channel_id(peer_id),
                                    access_hash=0
                                )
                            ]
                        )
                    )).chats
                )""",
        "B2 chat/channel 分支补 fetch_peers",
    )

    append_once(
        CLIENT,
        '''

# [patch] B4: session 落盘目录可配置。
# 默认 workdir 是 "." (=容器内 /app, 临时层), 容器 recreate 就丢 auth key 与 peer 缓存
# -> 每次都要重新登录, 而且新 session 解析不了群/频道; 反复登录还会触发 Telegram
# 登录限流([420 FLOOD_WAIT_X] ... auth.ImportBotAuthorization)。给容器一个
# BOT_SESSION_DIR 就能把它挂到持久化卷上。
def _session_dir_patch():
    import os as _os

    _d = _os.environ.get("BOT_SESSION_DIR")
    if not _d:
        return
    try:
        _os.makedirs(_d, exist_ok=True)
    except Exception:
        return
    _orig_init = Client.__init__

    def __init__(self, name, *a, **k):
        k.setdefault("workdir", _d)
        _orig_init(self, name, *a, **k)

    Client.__init__ = __init__


_session_dir_patch()
''',
        "_session_dir_patch",
        "B4 session 目录可配置(BOT_SESSION_DIR)",
    )

    # 校验: 语法 + 标记 + 真实行为
    import py_compile
    for p in (SESSION, RESOLVE, UTILS, CLIENT):
        py_compile.compile(p, doraise=True)

    src = io.open(SESSION, encoding="utf-8").read()
    for marker in ("_restarting_guard", "Ping 连续失败 -> 重建会话",
                   "传输层已关 -> 重建会话", "Session start failed"):
        assert marker in src, "缺标记: " + marker
    assert "fetch_peers(" in io.open(RESOLVE, encoding="utf-8").read()
    assert "_session_dir_patch" in io.open(CLIENT, encoding="utf-8").read()

    from pyrogram import utils
    cases = [(-1002257538689, "channel"), (-1000000000001, "channel"),
             (-1002147483648, "channel"), (-1000000000, "chat"), (7528636189, "user")]
    for pid, want in cases:
        got = utils.get_peer_type(pid)
        assert got == want, "get_peer_type(%s) = %s, 期望 %s" % (pid, got, want)
    print("  [ok] get_peer_type: " + ", ".join("%s->%s" % (p, t) for p, t in cases))

    print("\n全部补丁已应用并通过语法/标记/行为校验")
    print("pyrogram 版本: %s" % pyrogram.__version__)


if __name__ == "__main__":
    main()
