import unittest

from client_mod_bench.tracker import classify_process, matches_name


class MatchNameTest(unittest.TestCase):
    def test_windows_and_linux_names(self):
        self.assertTrue(matches_name("Discord.exe", ["Discord"]))
        self.assertTrue(matches_name("discord", ["Discord"]))
        self.assertTrue(matches_name("EndcordDesktop.exe", ["EndcordDesktop"]))

    def test_other_builds_do_not_match(self):
        self.assertFalse(matches_name("DiscordPTB.exe", ["Discord"]))
        self.assertFalse(matches_name("DiscordCanary", ["Discord"]))

    def test_macos_helpers(self):
        self.assertTrue(matches_name("Discord Helper (Renderer)", ["Discord"]))
        self.assertTrue(matches_name("Discord PTB Helper (GPU)", ["Discord PTB"]))
        self.assertFalse(matches_name("Discord PTB Helper (GPU)", ["Discord"]))


class ClassifyTest(unittest.TestCase):
    def test_chromium_process_types(self):
        exe = "Discord.exe"
        self.assertEqual(classify_process(exe, [exe]), "browser")
        self.assertEqual(classify_process(exe, [exe, "--type=renderer", "--x"]), "renderer")
        self.assertEqual(classify_process(exe, [exe, "--type=gpu-process"]), "gpu")
        self.assertEqual(classify_process(exe, [exe, "--type=utility"]), "utility")
        self.assertEqual(classify_process(exe, [exe, "--type=crashpad-handler"]), "crashpad")

    def test_separate_crashpad_binary(self):
        self.assertEqual(classify_process("chrome_crashpad_handler", ["x", "--monitor-self"]),
                         "crashpad")


if __name__ == "__main__":
    unittest.main()
