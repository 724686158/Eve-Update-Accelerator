"""单元测试：全部离线，用假网络把决策逻辑钉死。

三条铁律（都是踩坑总结）：

1. **不许碰真实网络**。测速/探活全都注入假的 fetch，否则 CI 会因网络抖动而红。
2. **不许碰系统 hosts**。一律用临时文件，测试跑完系统配置一字未动。
3. **决策逻辑要有反例测试**。例如「所有落点都取不到内容时必须拒绝写入」——
   这正是第一版会犯的错（只看握手就认为可用）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from eveupdate import elevate, evindex, hosts, scan  # noqa: E402


# --------------------------------------------------------------------------
# hosts：文本层面的所有不变量
# --------------------------------------------------------------------------
class HostsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "hosts")
        self.base = "##\n# Host Database\n127.0.0.1\tlocalhost\n::1 localhost\n"
        self.write(self.base)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def write(self, text: str, *, newline: str = "\n", encoding: str = "utf-8") -> None:
        body = text.replace("\n", newline) if newline != "\n" else text
        with open(self.path, "w", encoding=encoding, newline="") as fh:
            fh.write(body)

    def test_read_missing_file_is_not_an_error(self) -> None:
        missing = hosts.HostsFile.read(os.path.join(self.dir.name, "nope"))
        self.assertFalse(missing.exists)
        self.assertEqual(missing.pinned_ips(), [])

    def test_block_install_update_revert_roundtrip(self) -> None:
        plan = hosts.plan_apply(["1.1.1.1", "2.2.2.2"], hosts_path=self.path)
        self.assertEqual(plan.action, "install")
        hosts.apply_plan_as_root(plan)
        self.assertEqual(hosts.HostsFile.read(self.path).pinned_ips(), ["1.1.1.1", "2.2.2.2"])

        # 幂等：同一组 IP 再写一次，判定为「无需改动」
        again = hosts.plan_apply(["1.1.1.1", "2.2.2.2"], hosts_path=self.path)
        self.assertFalse(again.changed)

        # 换 IP → update
        changed = hosts.plan_apply(["3.3.3.3"], hosts_path=self.path)
        self.assertEqual(changed.action, "update")
        hosts.apply_plan_as_root(changed)
        self.assertEqual(hosts.HostsFile.read(self.path).pinned_ips(), ["3.3.3.3"])

        # 撤销 → 块消失，原有内容一字不差
        revert = hosts.plan_revert(hosts_path=self.path)
        hosts.apply_plan_as_root(revert)
        after = hosts.HostsFile.read(self.path)
        self.assertIsNone(after.block())
        self.assertIn("127.0.0.1\tlocalhost", after.normalized)
        self.assertNotIn("3.3.3.3", after.normalized)

    def test_only_one_block_after_repeated_writes(self) -> None:
        for ips in (["1.1.1.1"], ["2.2.2.2"], ["3.3.3.3"]):
            hosts.apply_plan_as_root(hosts.plan_apply(ips, hosts_path=self.path))
        text = hosts.HostsFile.read(self.path).normalized
        self.assertEqual(text.count(hosts.MARK_BEGIN), 1)
        # 只数「记录行」：注释里出现域名是正常的（说明块是干什么的）
        records = [
            line for line in text.split("\n")
            if line.strip() and not line.lstrip().startswith("#")
            and "binaries.eveonline.com" in line
        ]
        self.assertEqual(len(records), 1, records)

    def test_crlf_is_preserved(self) -> None:
        self.write(self.base, newline="\r\n")
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path)
        self.assertIn("\r\n", plan.new_text)
        # 不能出现「光杆 \n」：那说明文件被混了两种换行风格
        self.assertNotIn("\n", plan.new_text.replace("\r\n", ""))

    def test_bom_is_preserved_exactly_once(self) -> None:
        self.write("\ufeff" + self.base, encoding="utf-8")
        self.assertTrue(hosts.HostsFile.read(self.path).had_bom)
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path)
        self.assertTrue(plan.new_text.startswith("\ufeff"))
        # lstrip("\ufeff") 会把连续多个也吃掉，必须是只去掉一个
        self.assertFalse(plan.new_text.startswith("\ufeff\ufeff"))

    def test_backup_created_once_and_never_overwritten(self) -> None:
        hosts.apply_plan_as_root(hosts.plan_apply(["1.1.1.1"], hosts_path=self.path))
        backup = hosts.backup_path(self.path)
        self.assertTrue(os.path.exists(backup))
        with open(backup, encoding="utf-8") as fh:
            first = fh.read()
        hosts.apply_plan_as_root(hosts.plan_apply(["9.9.9.9"], hosts_path=self.path))
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first)
        self.assertNotIn("1.1.1.1", first)  # 备份是「改动前」的内容

    def test_revert_removes_block_without_leaving_blank_lines(self) -> None:
        hosts.apply_plan_as_root(hosts.plan_apply(["1.1.1.1"], hosts_path=self.path))
        plan = hosts.plan_revert(hosts_path=self.path)
        hosts.apply_plan_as_root(plan)
        text = hosts.HostsFile.read(self.path).normalized
        self.assertNotIn("\n\n\n", text)
        self.assertFalse(text.endswith("\n\n"))

    def test_revert_without_block_is_noop(self) -> None:
        plan = hosts.plan_revert(hosts_path=self.path)
        self.assertFalse(plan.changed)
        self.assertEqual(plan.action, "noop")
        self.assertTrue(plan.notes)

    def test_pinned_ips_tolerates_manual_edits(self) -> None:
        self.write(
            self.base
            + f"{hosts.MARK_BEGIN}\n# comment\n1.1.1.1   2.2.2.2\tbinaries.eveonline.com\n"
            + f"{hosts.MARK_END}\n"
        )
        self.assertEqual(hosts.HostsFile.read(self.path).pinned_ips(), ["1.1.1.1", "2.2.2.2"])

    def test_plan_apply_rejects_garbage(self) -> None:
        with self.assertRaises(hosts.HostsError):
            hosts.plan_apply(["not-an-ip"], hosts_path=self.path)

    def test_default_hosts_path_per_platform(self) -> None:
        self.assertEqual(hosts.default_hosts_path("darwin"), "/etc/hosts")
        self.assertIn("System32", hosts.default_hosts_path("win32"))
        self.assertIn("drivers", hosts.default_hosts_path("win32"))


# --------------------------------------------------------------------------
# evindex：基准资源必须来自真实索引，避免「硬编码 URL 腐烂」
# --------------------------------------------------------------------------
class EvIndexTests(unittest.TestCase):
    def test_parse_line_accepts_real_format(self) -> None:
        line = "app:/EVE.app/Contents/Info.plist,a3/a374381be814f746_abcdef,sha,841,420,33188"
        self.assertEqual(
            evindex.parse_index_line(line), ("/a3/a374381be814f746_abcdef", 841)
        )

    def test_parse_line_rejects_local_paths_and_junk(self) -> None:
        for bad in (
            "",
            "no,commas",
            "app:/x,y,z,notanumber",
            "app:/x,local/path/with/too/many/slashes,sha,10",
            "app:/x,,sha,10",
            "app:/x,ab/cd,sha,0",
        ):
            self.assertIsNone(evindex.parse_index_line(bad), msg=bad)

    def test_biggest_resource_picks_the_largest(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("a,b/c_d,sha,100\n")
            fh.write("a,e/f_g,sha,9000000\n")
            fh.write("a,h/i_j,sha,5000\n")
            path = fh.name
        try:
            target = evindex.biggest_resource(path, min_bytes=1000)
            self.assertIsNotNone(target)
            assert target is not None
            self.assertEqual(target.path, "/e/f_g")
            self.assertEqual(target.size, 9000000)
            self.assertFalse(target.stale_risk)
        finally:
            os.unlink(path)

    def test_biggest_resource_respects_min_bytes(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("a,b/c_d,sha,100\n")
            path = fh.name
        try:
            self.assertIsNone(evindex.biggest_resource(path, min_bytes=1000))
        finally:
            os.unlink(path)

    def test_find_index_accepts_directory_or_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            index = os.path.join(d, evindex.INDEX_NAME)
            open(index, "w").close()
            self.assertEqual(evindex.find_index(d), index)
            self.assertEqual(evindex.find_index(index), index)
            self.assertIsNone(evindex.find_index(os.path.join(d, "missing.txt")))
            self.assertIsNone(evindex.find_index(d + "-nope"))

    def test_builtin_fallback_is_flagged_as_stale(self) -> None:
        target = evindex.bench_target(explicit_index="/nonexistent/path")
        self.assertTrue(target.stale_risk)
        self.assertEqual(target.source, "builtin")

    def test_windows_candidates_include_localappdata(self) -> None:
        dirs = evindex.cache_dirs("win32")
        self.assertTrue(any("SharedCache" in d for d in dirs))


# --------------------------------------------------------------------------
# scan：决策逻辑（全部离线）
# --------------------------------------------------------------------------
def fake_fetch_factory(ok_ips=(), body=b"x" * 1000):
    """造一个假 fetch：只有 ok_ips 里的地址能拿到内容。

    刻意用 ``*args, **kwargs`` 而不是复刻 bench_fetch 的签名：真实调用方
    （probe_content / measure_throughput）混用位置与关键字传参，手写签名很容易
    撞成「got multiple values for argument 'target'」。假函数只需要认 ip。
    """

    def fetch(ip, *_args, **_kwargs):
        if ip in ok_ips:
            return len(body), 206, {"x-amz-cf-pop": "LAX50"}
        return 17, 530, {}


    return fetch


class ScanDecisionTests(unittest.TestCase):
    def test_doh_parser_filters_to_the_cname_chain(self) -> None:
        payload = (
            b'{"Status":0,"Answer":['
            b'{"name":"binaries.eveonline.com.","type":5,"data":"d1.cloudfront.net."},'
            b'{"name":"d1.cloudfront.net.","type":1,"data":"18.65.14.46"},'
            b'{"name":"unrelated.example.","type":1,"data":"9.9.9.9"}]}'
        )
        self.assertEqual(
            scan.parse_doh_addresses(payload, host="binaries.eveonline.com"),
            ["18.65.14.46"],
        )
        # 不传 host 时不做过滤（保持原行为，便于别的用途）
        self.assertEqual(len(scan.parse_doh_addresses(payload)), 2)

    def test_doh_parser_handles_garbage(self) -> None:
        for bad in (b"", b"not json", b"{}", b'{"Answer":null}', b"[]"):
            self.assertEqual(scan.parse_doh_addresses(bad), [])

    def test_probe_content_distinguishes_530_from_success(self) -> None:
        dead = scan.probe_content("1.1.1.1", scan.BUILTIN_BENCH, bench=fake_fetch_factory())
        self.assertFalse(dead.ok)
        self.assertEqual(dead.status, 530)
        self.assertIn("530", dead.detail)

        alive = scan.probe_content(
            "2.2.2.2", scan.BUILTIN_BENCH, bench=fake_fetch_factory(ok_ips={"2.2.2.2"})
        )
        self.assertTrue(alive.ok)
        self.assertEqual(alive.recv_bytes, 1000)
        self.assertEqual(alive.colo, "LAX")  # 从 x-amz-cf-pop 提取

    def test_probe_content_survives_exceptions(self) -> None:
        def boom(*_a, **_k):
            raise TimeoutError("nope")

        probe = scan.probe_content("1.1.1.1", scan.BUILTIN_BENCH, bench=boom)
        self.assertFalse(probe.ok)
        self.assertIn("TimeoutError", probe.detail)

    def test_scan_refuses_to_pin_when_nothing_serves_content(self) -> None:
        """最重要的一条反例：所有落点都取不到内容时，必须一个都不推荐。"""
        result = scan.scan(
            top=2,
            conns=2,
            extra_ips=["1.1.1.1"],
            seeds=(),       # 候选只有 extra_ips
            resolvers=(),
            dns_ips=[],
            doh_fetch=lambda url, *, timeout=None, headers=None: (
                200, b'{"Answer":[]}', {}
            ),
            bench=fake_fetch_factory(ok_ips=set()),
            log=None,
        )
        self.assertEqual(result.measured, [])
        self.assertEqual(result.to_pin(), [])
        self.assertTrue(result.content_failed)

    def test_scan_picks_the_fastest_working_endpoint(self) -> None:
        # bench 是 HTTP 层注入：返回 (字节数, 状态码, 响应头)。
        # 1.1.1.1 既快又能用；2.2.2.2 能用但慢；3.3.3.3 取不到内容。
        def bench(ip, *_args, **_kwargs):
            if ip == "1.1.1.1":
                return 4096, 206, {"x-amz-cf-pop": "LAX50"}
            if ip == "2.2.2.2":
                return 1024, 206, {}
            return 17, 530, {}

        result = scan.scan(
            top=4,
            conns=2,
            previous_ips=[],
            # 用 extra_ips 注入候选：这就是让工具「只测我指定的地址」的正式接口
            extra_ips=["1.1.1.1", "2.2.2.2", "3.3.3.3"],
            seeds=(),       # 不用种子网段
            resolvers=(),   # 不查系统解析器（那会用 dig 打真网络）
            dns_ips=[],     # 不读系统 DNS
            doh_fetch=lambda url, *, timeout=None, headers=None: (
                200, b'{"Answer":[]}', {}
            ),
            bench=bench,
            log=None,
        )
        self.assertEqual(result.best_ip, "1.1.1.1")
        self.assertEqual(result.content_failed, ["3.3.3.3"])
        self.assertEqual(result.to_pin()[0], "1.1.1.1")

    def test_pinned_failing_detects_a_dead_pin(self) -> None:
        """现役落点取不到内容时必须能被识别出来 —— 这就是「更新卡住」的真身。"""
        result = scan.ScanResult(host=scan.TARGET_HOST, previous_ips=["1.1.1.1"])
        result.content_failed = ["1.1.1.1"]
        self.assertEqual(result.pinned_failing(), "1.1.1.1")
        ok, message = result.pinned_status()
        self.assertFalse(ok)
        self.assertIn("无法下载内容", message)

    def test_pinned_status_keeps_a_good_pin(self) -> None:
        result = scan.ScanResult(host=scan.TARGET_HOST, previous_ips=["1.1.1.1"])
        result.measured = [
            scan.Throughput(ip="1.1.1.1", recv_bytes=2_000_000, seconds=1.0, ok_parts=2, parts=2),
            scan.Throughput(ip="2.2.2.2", recv_bytes=2_100_000, seconds=1.0, ok_parts=2, parts=2),
        ]
        ok, message = result.pinned_status()
        self.assertTrue(ok)
        self.assertIn("最优", message)

    def test_pinned_status_suggests_switching_when_much_slower(self) -> None:
        result = scan.ScanResult(host=scan.TARGET_HOST, previous_ips=["1.1.1.1"])
        result.measured = [
            scan.Throughput(ip="2.2.2.2", recv_bytes=5_000_000, seconds=1.0, ok_parts=2, parts=2),
            scan.Throughput(ip="1.1.1.1", recv_bytes=500_000, seconds=1.0, ok_parts=2, parts=2),
        ]
        ok, message = result.pinned_status()
        self.assertFalse(ok)
        self.assertIn("建议重跑", message)

    def test_to_pin_puts_best_first_then_dead_then_current(self) -> None:
        result = scan.ScanResult(host=scan.TARGET_HOST, previous_ips=["9.9.9.9"])
        result.measured = [
            scan.Throughput(ip="2.2.2.2", recv_bytes=100, seconds=1.0, ok_parts=1, parts=1)
        ]
        result.dead_ips = ["3.3.3.3"]
        self.assertEqual(result.to_pin(), ["2.2.2.2", "3.3.3.3", "9.9.9.9"])

    def test_measure_throughput_sums_only_successful_parts(self) -> None:
        import time as _time

        calls = []

        def bench(ip, *args, **kwargs):
            calls.append(kwargs.get("slot", 0))
            if kwargs.get("slot", 0) == 0:
                return 0, 530, {}
            _time.sleep(0.01)
            return 1000, 206, {}

        result = scan.measure_throughput("1.1.1.1", conns=4, fetch=bench)
        self.assertEqual(result.ok_parts, 3)
        self.assertEqual(result.parts, 4)
        self.assertEqual(result.recv_bytes, 3000)
        self.assertFalse(result.complete)
        self.assertEqual(sorted(calls), [0, 1, 2, 3])

    def test_edge_from_headers_reads_both_cdns(self) -> None:
        self.assertEqual(scan.edge_from_headers({"x-amz-cf-pop": "LAX50"}), "LAX")
        self.assertEqual(scan.edge_from_headers({"cf-ray": "abc123-LHR"}), "LHR")
        self.assertEqual(scan.edge_from_headers({}), "")
        self.assertEqual(scan.edge_from_headers(None), "")

    def test_unpack_fetch_accepts_two_and_three_tuples(self) -> None:
        self.assertEqual(scan._unpack_fetch((1, 206)), (1, 206, {}))
        self.assertEqual(scan._unpack_fetch((1, 206, {"a": "b"})), (1, 206, {"a": "b"}))

    def test_resolver_command_is_platform_correct(self) -> None:
        self.assertEqual(
            scan.resolver_command("8.8.8.8", "x.com", platform="win32"),
            ["nslookup", "-type=A", "x.com", "8.8.8.8"],
        )
        posix = scan.resolver_command("8.8.8.8", "x.com", platform="darwin")
        self.assertIn("x.com", posix)
        # dig 的服务器是 "@host" 形态（nslookup 才是独立参数），两种都要认
        self.assertTrue(any("8.8.8.8" in part for part in posix), posix)


# --------------------------------------------------------------------------
# elevate：命令构造与验证判定（不执行提权）
# --------------------------------------------------------------------------
class ElevateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "hosts")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("# base\n127.0.0.1 localhost\n")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_platform_tag(self) -> None:
        self.assertEqual(elevate.platform_tag("win32"), "windows")
        self.assertEqual(elevate.platform_tag("darwin"), "macos")
        self.assertEqual(elevate.platform_tag("linux"), "linux")
        self.assertEqual(elevate.platform_tag("freebsd"), "other")

    def test_windows_command_uses_powershell_and_flushes_dns(self) -> None:
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path)
        argv = elevate.build_command(plan, r"C:\Temp\staged.hosts", platform="win32")
        self.assertEqual(argv[0], "powershell.exe")
        joined = " ".join(argv)
        self.assertIn(r"C:\Temp\staged.hosts", joined)
        self.assertIn("Copy-Item", joined)
        self.assertIn("flushdns", joined)

    def test_macos_command_writes_and_flushes(self) -> None:
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path)
        argv = elevate.build_command(plan, "/tmp/staged.hosts", platform="darwin")
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        shell = argv[2]
        self.assertIn("/bin/cat /tmp/staged.hosts", shell)
        self.assertIn(self.path, shell)
        self.assertIn("dscacheutil", shell)
        self.assertTrue(shell.endswith("true"))

    def test_paths_with_spaces_are_quoted(self) -> None:
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path="/tmp/dir with space/hosts")
        argv = elevate.build_command(plan, "/tmp/staged file.hosts", platform="darwin")
        self.assertIn("'/tmp/staged file.hosts'", argv[2])
        self.assertIn("'/tmp/dir with space/hosts'", argv[2])

    def test_dry_run_never_touches_anything(self) -> None:
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path, dry_run=True)
        with open(self.path, encoding="utf-8") as fh:
            before = fh.read()
        result = elevate.run(plan)
        self.assertTrue(result.ok)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_verify_detects_applied_update_and_revert(self) -> None:
        plan = hosts.plan_apply(["1.1.1.1", "2.2.2.2"], hosts_path=self.path)
        hosts.apply_plan_as_root(plan)
        ok, message = elevate.verify(plan)
        self.assertTrue(ok, message)
        self.assertIn("已生效", message)

        revert = hosts.plan_revert(hosts_path=self.path)
        hosts.apply_plan_as_root(revert)
        ok, message = elevate.verify(revert)
        self.assertTrue(ok, message)

    def test_verify_reports_failure_when_file_was_restored(self) -> None:
        """安全软件把 hosts 还原时，验证必须报错而不是假装成功。"""
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path)
        # 不落盘，直接验证：应当失败，因为文件里根本没有块
        ok, message = elevate.verify(plan)
        self.assertFalse(ok)
        self.assertIn("不符", message)

    def test_verify_noop_when_already_target_state(self) -> None:
        hosts.apply_plan_as_root(hosts.plan_apply(["1.1.1.1"], hosts_path=self.path))
        plan = hosts.plan_apply(["1.1.1.1"], hosts_path=self.path)
        self.assertFalse(plan.changed)
        ok, message = elevate.verify(plan)
        self.assertTrue(ok, message)

    def test_describe_channel_mentions_a_real_mechanism(self) -> None:
        for platform in ("win32", "darwin", "linux"):
            text = elevate.describe_channel(platform)
            self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()


class GuiLanguageTests(unittest.TestCase):
    """语言选择是纯逻辑，必须离线可测（tkinter 不参与）。"""

    def test_explicit_choice_wins(self) -> None:
        from eveupdate import gui

        self.assertEqual(gui.pick_language("zh"), "zh")
        self.assertEqual(gui.pick_language("en"), "en")
        self.assertEqual(gui.pick_language("fr"), gui.pick_language(None))  # 未知值走自动

    def test_env_var_drives_choice(self) -> None:
        import os

        from eveupdate import gui

        old = {k: os.environ.get(k) for k in ("LANG", "LC_ALL", "LC_MESSAGES")}
        try:
            for key in old:
                os.environ.pop(key, None)
            os.environ["LANG"] = "zh_CN.UTF-8"
            self.assertEqual(gui.pick_language(), "zh")
            os.environ["LANG"] = "en_US.UTF-8"
            self.assertEqual(gui.pick_language(), "en")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_platform_hint_beats_unreliable_locale(self) -> None:
        """实测：中文 macOS 上 locale.getlocale() 会给 en_US（C 库兜底值），
        所以平台原生设置必须优先于它，否则中文用户会看到英文界面。"""
        import os
        from unittest import mock

        from eveupdate import gui

        old = {k: os.environ.get(k) for k in ("LANG", "LC_ALL", "LC_MESSAGES")}
        try:
            for key in old:
                os.environ.pop(key, None)
            with mock.patch.object(gui, "_platform_locale_hint", return_value="zh_CN"):
                with mock.patch("locale.getlocale", return_value=("en_US", "UTF-8")):
                    self.assertEqual(gui.pick_language(), "zh")
            # 但用户显式指定 LANG=en_US 时，仍要尊重用户
            os.environ["LANG"] = "en_US.UTF-8"
            with mock.patch.object(gui, "_platform_locale_hint", return_value="zh_CN"):
                self.assertEqual(gui.pick_language(), "en")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_string_tables_are_symmetric(self) -> None:
        from eveupdate import gui

        self.assertEqual(set(gui.STRINGS["zh"]), set(gui.STRINGS["en"]))
        for table in gui.STRINGS.values():
            for key, value in table.items():
                self.assertTrue(value.strip(), key)
