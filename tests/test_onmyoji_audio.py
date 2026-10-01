from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import onmyoji_audio
from onmyoji_audio import AudioSample, build_audio_index, find_audio_matches, parse_fsb5_samples


def _write_test_fsb(path: Path) -> None:
    names = b"axl_born_vo\0axl_skill0_vo\0"
    offsets = struct.pack("<II", 8, 20)
    # Two simple FSB5 sample headers, each with no extension chunks.
    header = bytearray(0x3C)
    header[:4] = b"FSB5"
    struct.pack_into("<IIIIII", header, 4, 1, 2, 16, len(offsets) + len(names), 0, 15)
    path.write_bytes(bytes(header) + b"\0" * 16 + offsets + names)


class AudioIndexTests(unittest.TestCase):
    def test_find_decoder_in_extracted_windows_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            decoder = root / "tools" / "vgmstream-win" / "vgmstream-cli.exe"
            decoder.parent.mkdir(parents=True)
            decoder.touch()
            self.assertEqual(onmyoji_audio.find_vgmstream_cli(root), decoder)

    def test_parse_and_cache_fsb5_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank = root / "g37_creature_axl_vo.fsb"
            _write_test_fsb(bank)
            self.assertEqual(parse_fsb5_samples(bank), (15, ["axl_born_vo", "axl_skill0_vo"]))
            cache = root / "audio.json"
            first = build_audio_index(root, cache)
            second = build_audio_index(root, cache)
            self.assertEqual(first, second)
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["version"], 1)

    def test_match_uses_model_code_and_action(self) -> None:
        bank = Path("g37_creature_axl_vo.fsb")
        samples = [
            AudioSample(bank, 1, "axl_born_vo"),
            AudioSample(bank, 2, "axl_skill0_vo"),
            AudioSample(Path("g37_creature_other_vo.fsb"), 1, "other_born_vo"),
        ]
        header = SimpleNamespace(action="born", skeleton_name="axiuluo")
        matches = find_audio_matches(header, samples)
        self.assertEqual([item.sample.name for item in matches], ["axl_born_vo"])

    def test_export_calls_decoder_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank = root / "g37_creature_axl_vo.fsb"
            _write_test_fsb(bank)
            sounds = root / "sounds"
            sounds.mkdir()
            bank.replace(sounds / bank.name)
            workspace = root / "workspace"
            output = root / "out"
            header = SimpleNamespace(
                action="born", skeleton_name="axiuluo", path=root / "motion.rawanimation"
            )

            def fake_run(command, **_kwargs):
                target = Path(command[command.index("-o") + 1])
                target.write_bytes(b"RIFF")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(onmyoji_audio, "find_sound_root", return_value=sounds), patch.object(
                onmyoji_audio, "find_vgmstream_cli", return_value=root / "vgmstream-cli.exe"
            ), patch.object(onmyoji_audio.subprocess, "run", side_effect=fake_run):
                files = onmyoji_audio.export_motion_audio(header, output, workspace=workspace)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].is_file())
            self.assertTrue((output / "audio_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
