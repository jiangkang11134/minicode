"""Tests for single-file fizzbuzz.py (all 6 levels + CLI)."""
import pytest
from fizzbuzz import (
    fizzbuzz,
    fizzbuzz_with_rules,
    FizzBuzz,
    fizzbuzz_range,
    fizzbuzz_reverse,
    fizzbuzz_stats,
    format_sequence,
    parse_rules,
)


# ─── Level 1: Basic ──────────────────────────────────────────────────────────

class TestLevel1:
    def test_first_15(self):
        assert fizzbuzz(15) == [
            1, 2, "Fizz", 4, "Buzz", "Fizz",
            7, 8, "Fizz", "Buzz", 11, "Fizz",
            13, 14, "FizzBuzz",
        ]

    def test_first_1(self):
        assert fizzbuzz(1) == [1]

    def test_first_3(self):
        assert fizzbuzz(3) == [1, 2, "Fizz"]

    def test_first_5(self):
        assert fizzbuzz(5) == [1, 2, "Fizz", 4, "Buzz"]

    def test_fizzbuzz_positions(self):
        result = fizzbuzz(100)
        positions = [i for i, v in enumerate(result, 1) if v == "FizzBuzz"]
        assert positions == [15, 30, 45, 60, 75, 90]

    def test_invalid_type(self):
        with pytest.raises(TypeError, match="must be an integer"):
            fizzbuzz("10")

    def test_zero(self):
        with pytest.raises(ValueError, match="at least 1"):
            fizzbuzz(0)

    def test_negative(self):
        with pytest.raises(ValueError, match="at least 1"):
            fizzbuzz(-5)

    def test_length(self):
        for n in [1, 3, 5, 15, 100]:
            assert len(fizzbuzz(n)) == n


# ─── Level 2: Custom Rules ───────────────────────────────────────────────────

class TestLevel2:
    def test_standard_rules(self):
        result = fizzbuzz_with_rules(15, {3: "Fizz", 5: "Buzz"})
        assert result == [
            1, 2, "Fizz", 4, "Buzz", "Fizz",
            7, 8, "Fizz", "Buzz", 11, "Fizz",
            13, 14, "FizzBuzz",
        ]

    def test_single_rule(self):
        assert fizzbuzz_with_rules(10, {2: "Even"}) == [
            1, "Even", 3, "Even", 5, "Even", 7, "Even", 9, "Even",
        ]

    def test_multiple_concat(self):
        result = fizzbuzz_with_rules(6, {2: "A", 3: "B"})
        assert result == [1, "A", "B", "A", 5, "AB"]

    def test_custom_words(self):
        result = fizzbuzz_with_rules(4, {2: "Foo", 4: "Bar"})
        assert result == [1, "Foo", 3, "FooBar"]

    def test_empty_rules(self):
        with pytest.raises(ValueError, match="must not be empty"):
            fizzbuzz_with_rules(10, {})

    def test_invalid_rules_type(self):
        with pytest.raises(TypeError, match="rules must be a dict"):
            fizzbuzz_with_rules(10, [3, "Fizz"])

    def test_zero_divisor(self):
        with pytest.raises(ValueError, match="divisor must be a positive integer"):
            fizzbuzz_with_rules(10, {0: "Zero"})

    def test_negative_divisor(self):
        with pytest.raises(ValueError, match="divisor must be a positive integer"):
            fizzbuzz_with_rules(10, {-3: "Bad"})

    def test_large_divisor(self):
        assert fizzbuzz_with_rules(3, {100: "Never"}) == [1, 2, 3]

    def test_deterministic_order(self):
        r1 = fizzbuzz_with_rules(6, {3: "A", 2: "B"})
        r2 = fizzbuzz_with_rules(6, {2: "B", 3: "A"})
        assert r1 == r2


# ─── Level 3: Class ──────────────────────────────────────────────────────────

class TestLevel3:
    def test_default_rules(self):
        fb = FizzBuzz()
        assert fb.generate(5) == [1, 2, "Fizz", 4, "Buzz"]

    def test_custom_rules(self):
        fb = FizzBuzz({2: "Even"})
        assert fb.generate(4) == [1, "Even", 3, "Even"]

    def test_default_output_string(self):
        fb = FizzBuzz({3: "Fizz"}, default_output=".")
        assert fb.generate(6) == [".", ".", "Fizz", ".", ".", "Fizz"]

    def test_default_output_none(self):
        fb = FizzBuzz({3: "Fizz"})
        assert fb.generate(3) == [1, 2, "Fizz"]

    def test_add_rule(self):
        fb = FizzBuzz({3: "Fizz"})
        fb.add_rule(5, "Buzz")
        assert fb.generate(15)[14] == "FizzBuzz"

    def test_remove_rule(self):
        fb = FizzBuzz({3: "Fizz", 5: "Buzz"})
        fb.remove_rule(5)
        result = fb.generate(15)
        assert result[4] == 5
        assert result[14] == "Fizz"
        assert all("Buzz" not in str(v) for v in result)

    def test_remove_nonexistent(self):
        fb = FizzBuzz()
        with pytest.raises(KeyError):
            fb.remove_rule(99)

    def test_add_rule_invalid(self):
        fb = FizzBuzz()
        with pytest.raises(ValueError, match="divisor must be a positive integer"):
            fb.add_rule(0, "Bad")

    def test_clear_rules(self):
        fb = FizzBuzz({3: "Fizz", 5: "Buzz"})
        fb.clear_rules()
        assert fb.generate(5) == [1, 2, 3, 4, 5]

    def test_matches_basic(self):
        fb = FizzBuzz()
        for n in [1, 3, 5, 10, 15, 50]:
            assert fb.generate(n) == fizzbuzz(n)

    def test_invalid_rules_type(self):
        with pytest.raises(TypeError, match="rules must be a dict"):
            FizzBuzz("not_a_dict")

    def test_negative_n(self):
        fb = FizzBuzz()
        with pytest.raises(ValueError, match="at least 1"):
            fb.generate(0)

    def test_empty_after_remove(self):
        fb = FizzBuzz({3: "Fizz"})
        fb.remove_rule(3)
        assert fb.generate(5) == [1, 2, 3, 4, 5]


# ─── Level 4: Range ──────────────────────────────────────────────────────────

class TestLevel4:
    def test_range_10_15(self):
        assert fizzbuzz_range(10, 15) == ["Buzz", 11, "Fizz", 13, 14, "FizzBuzz"]

    def test_range_single(self):
        assert fizzbuzz_range(3, 3) == ["Fizz"]

    def test_range_with_default(self):
        result = fizzbuzz_range(1, 4, {3: "Fizz"}, default_output=".")
        assert result == [".", ".", "Fizz", "."]

    def test_range_invalid_start(self):
        with pytest.raises(ValueError, match="at least 1"):
            fizzbuzz_range(0, 5)

    def test_range_invalid_stop(self):
        with pytest.raises(ValueError, match="stop must be >= start"):
            fizzbuzz_range(5, 3)

    def test_class_generate_range(self):
        fb = FizzBuzz()
        assert fb.generate_range(10, 15) == ["Buzz", 11, "Fizz", 13, 14, "FizzBuzz"]


# ─── Level 5: Reverse ────────────────────────────────────────────────────────

class TestLevel5:
    def test_reverse_5(self):
        assert fizzbuzz_reverse(5) == ["Buzz", 4, "Fizz", 2, 1]

    def test_reverse_15(self):
        result = fizzbuzz_reverse(15)
        assert result[0] == "FizzBuzz"
        assert result[-1] == 1
        assert len(result) == 15


# ─── Level 6: Stats ──────────────────────────────────────────────────────────

class TestLevel6:
    def test_stats_100(self):
        stats = fizzbuzz_stats(100)
        # Fizz 出现在所有 3 的倍数中（含 15 的倍数）
        # Buzz 出现在所有 5 的倍数中（含 15 的倍数）
        assert stats["total"] == 100
        assert stats["fizz"] == 33       # floor(100/3)
        assert stats["buzz"] == 20       # floor(100/5)
        assert stats["fizzbuzz"] == 6    # floor(100/15)
        assert stats["numbers"] == 53    # 100 - 33 - 20 + 6

    def test_stats_15(self):
        stats = fizzbuzz_stats(15)
        assert stats["total"] == 15
        assert stats["fizz"] == 5        # 3, 6, 9, 12, 15
        assert stats["buzz"] == 3        # 5, 10, 15
        assert stats["fizzbuzz"] == 1    # 15
        assert stats["numbers"] == 8

    def test_stats_custom(self):
        stats = fizzbuzz_stats(6, {2: "A", 3: "B"})
        assert stats["total"] == 6
        assert stats["a"] == 3       # 2, 4, 6
        assert stats["b"] == 2       # 3, 6
        assert stats["combined"] == 1  # 6 -> "AB"


# ─── Helper functions ────────────────────────────────────────────────────────

class TestHelpers:
    def test_format_sequence(self):
        seq = [1, 2, "Fizz", 4, "Buzz"]
        output = format_sequence(seq, cols=3)
        lines = output.strip().split("\n")
        assert len(lines) == 2  # 5 items / 3 cols = 2 lines

    def test_parse_rules(self):
        rules = parse_rules(["3:Fizz", "5:Buzz"])
        assert rules == {3: "Fizz", 5: "Buzz"}

    def test_parse_rules_invalid(self):
        with pytest.raises(ValueError, match="Invalid rule format"):
            parse_rules(["bad_format"])

    def test_parse_rules_non_int(self):
        with pytest.raises(ValueError, match="Invalid divisor"):
            parse_rules(["abc:Fizz"])

    def test_parse_rules_negative(self):
        with pytest.raises(ValueError, match="Divisor must be >= 1"):
            parse_rules(["-3:Fizz"])