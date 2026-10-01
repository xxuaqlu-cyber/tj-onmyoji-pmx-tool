from __future__ import annotations

import unittest

from onmyoji_resource_pull_gui import parse_mumu_instances


class MumuInstanceParsingTests(unittest.TestCase):
    def test_reads_running_instance_adb_port(self) -> None:
        output = (
            '{"0":{"index":"0","name":"MuMu","adb_host_ip":"127.0.0.1",'
            '"adb_port":16384,"is_process_started":true,"is_android_started":true},'
            '"1":{"index":"1","adb_port":16385,"is_process_started":false}}'
        )
        self.assertEqual(
            parse_mumu_instances(output),
            [{"serial": "127.0.0.1:16384", "name": "MuMu", "index": "0"}],
        )

    def test_ignores_android_that_is_still_starting(self) -> None:
        output = (
            '{"0":{"adb_port":16384,"is_process_started":true,'
            '"is_android_started":false}}'
        )
        self.assertEqual(parse_mumu_instances(output), [])


if __name__ == "__main__":
    unittest.main()
