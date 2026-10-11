import unittest
from greeting import greet


class GreetingTests(unittest.TestCase):
    def test_default_name(self):
        self.assertEqual(greet(), "Hello, world!")

    def test_named_greeting(self):
        self.assertEqual(greet("Agent"), "Hello, Agent!")


if __name__ == "__main__":
    unittest.main()
