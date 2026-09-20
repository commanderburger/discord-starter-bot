import unittest

from cogs.content_filter import contains_discord_link, find_slur, redact_slurs


class ContentFilterTests(unittest.TestCase):
    def test_common_obfuscations(self):
        for text in ("nigger", "n!gga", "n1gg3r", "n.i.g.g.a", "nіggа", "nègre"):
            with self.subTest(text=text):
                self.assertTrue(find_slur(text))
                self.assertEqual(redact_slurs(text), "[message redacted: prohibited slur]")

    def test_not_a_substring(self):
        for text in ("Nigeria", "Nigerian", "bigger", "niggardly", "normal conversation"):
            with self.subTest(text=text):
                self.assertFalse(find_slur(text))

    def test_discord_links(self):
        for text in ("https://discord.gg/example", "discord.com/invite/example", "discordapp.com/channels/123"):
            with self.subTest(text=text):
                self.assertTrue(contains_discord_link(text))
        for text in ("discord.com.evil.example", "notdiscord.gg", "a normal message"):
            with self.subTest(text=text):
                self.assertFalse(contains_discord_link(text))


if __name__ == "__main__":
    unittest.main()
