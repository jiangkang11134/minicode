"""Tests for FizzBuzz algorithm implementations (all three levels)."""
import pytest
from fizzbuzz_algorithms import fizzbuzz, fizzbuzz_with_rules, FizzBuzz


# ─── Level 1: Basic ──────────────────────────────────────────────────────────

class TestBasicFizzBuzz:
    """Test suite for level1_basic.fizzbuzz()."""

    def test_first_15(self):
        """Classic FizzBuzz from 1 to 15."""
        result = fizzbuzz(15)
        assert result == [
            1, 2, "Fizz", 4, "Buzz", "Fizz",
            7, 8, "Fizz", "Buzz", 11, "Fizz",
            13, 14, "FizzBuzz",
        ]

    def test_first_1(self):
        """Minimum valid input."""
        assert fizzbuzz(1) == [1]

    def test_first_3(self):
        """Single Fizz."""
        assert fizzbuzz(3) == [1, 2, "Fizz"]

    def test_first_5(self):
        """Single Buzz."""
        assert fizzbuzz(5) == [1, 2, "Fizz", 4, "Buzz"]

    def test_n_100_contains_fizzbuzz(self):
        """FizzBuzz appears at 15, 30, 45, etc."""
        result = fizzbuzz(100)
        fizzbuzz_positions = [i for i, v in enumerate(result, 1) if v == "FizzBuzz"]
        assert fizzbuzz_positions == [15, 30, 45, 60, 75, 90]

    def test_invalid_type(self):
        """Non-integer raises TypeError."""
        with pytest.raises(TypeError, match="must be an integer"):
            fizzbuzz("10")

    def test_zero(self):
        """n < 1 raises ValueError."""
        with pytest.raises(ValueError, match="at least 1"):
            fizzbuzz(0)

    def test_negative(self):
        """Negative n raises ValueError."""
        with pytest.raises(ValueError, match="at least 1"):
            fizzbuzz(-5)

    def test_no_fizz_before_3(self):
        """Numbers before 3 are just numbers."""
        result = fizzbuzz(2)
        assert result == [1, 2]

    def test_result_length(self):
        """Result length equals n."""
        for n in [1, 3, 5, 15, 100]:
            assert len(fizzbuzz(n)) == n


# ─── Level 2: Custom Rules ───────────────────────────────────────────────────

class TestCustomFizzBuzz:
    """Test suite for level2_custom.fizzbuzz_with_rules()."""

    def test_standard_rules(self):
        """Default-like rules produce classic output."""
        result = fizzbuzz_with_rules(15, {3: "Fizz", 5: "Buzz"})
        assert result == [
            1, 2, "Fizz", 4, "Buzz", "Fizz",
            7, 8, "Fizz", "Buzz", 11, "Fizz",
            13, 14, "FizzBuzz",
        ]

    def test_single_rule(self):
        """Single divisor rule — numbers not matching stay as themselves."""
        assert fizzbuzz_with_rules(10, {2: "Even"}) == [
            1, "Even", 3, "Even", 5, "Even", 7, "Even", 9, "Even",
        ]

    def test_multiple_rules_concat(self):
        """Multiple matching divisors produce concatenated output."""
        result = fizzbuzz_with_rules(6, {2: "A", 3: "B"})
        # 2 -> A, 3 -> B, 4 -> A, 6 -> A+B -> "AB"
        assert result == [1, "A", "B", "A", 5, "AB"]

    def test_custom_words(self):
        """Arbitrary words work."""
        result = fizzbuzz_with_rules(4, {2: "Foo", 4: "Bar"})
        # 2 -> Foo, 4 -> Foo+Bar -> "FooBar"
        assert result == [1, "Foo", 3, "FooBar"]

    def test_empty_rules(self):
        """Empty rules raises ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            fizzbuzz_with_rules(10, {})

    def test_invalid_rules_type(self):
        """Non-dict rules raises TypeError."""
        with pytest.raises(TypeError, match="rules must be a dict"):
            fizzbuzz_with_rules(10, [3, "Fizz"])

    def test_zero_divisor(self):
        """Divisor < 1 raises ValueError."""
        with pytest.raises(ValueError, match="divisor must be a positive integer"):
            fizzbuzz_with_rules(10, {0: "Zero"})

    def test_negative_divisor(self):
        """Negative divisor raises ValueError."""
        with pytest.raises(ValueError, match="divisor must be a positive integer"):
            fizzbuzz_with_rules(10, {-3: "Bad"})

    def test_divisor_greater_than_n(self):
        """Divisor larger than n — no matches, all numbers."""
        result = fizzbuzz_with_rules(3, {100: "Never"})
        assert result == [1, 2, 3]

    def test_rule_order_deterministic(self):
        """Output order is deterministic regardless of dict insertion order."""
        r1 = fizzbuzz_with_rules(6, {3: "A", 2: "B"})
        r2 = fizzbuzz_with_rules(6, {2: "B", 3: "A"})
        assert r1 == r2


# ─── Level 3: Class ──────────────────────────────────────────────────────────

class TestFizzBuzzClass:
    """Test suite for level3_advanced.FizzBuzz class."""

    def test_default_rules(self):
        """Default rules are {3: 'Fizz', 5: 'Buzz'}."""
        fb = FizzBuzz()
        assert fb.generate(5) == [1, 2, "Fizz", 4, "Buzz"]

    def test_custom_rules(self):
        """Custom rules in constructor — unmatched numbers stay as ints."""
        fb = FizzBuzz({2: "Even"})
        assert fb.generate(4) == [1, "Even", 3, "Even"]

    def test_default_output_string(self):
        """default_output replaces unmatched numbers."""
        fb = FizzBuzz({3: "Fizz"}, default_output=".")
        result = fb.generate(6)
        assert result == [".", ".", "Fizz", ".", ".", "Fizz"]

    def test_default_output_none(self):
        """default_output=None (default) uses the number itself."""
        fb = FizzBuzz({3: "Fizz"})
        result = fb.generate(3)
        assert result == [1, 2, "Fizz"]

    def test_add_rule(self):
        """add_rule dynamically adds a divisor→word mapping."""
        fb = FizzBuzz({3: "Fizz"})
        fb.add_rule(5, "Buzz")
        assert fb.generate(15)[14] == "FizzBuzz"

    def test_remove_rule(self):
        """remove_rule deletes an existing divisor."""
        fb = FizzBuzz({3: "Fizz", 5: "Buzz"})
        fb.remove_rule(5)
        result = fb.generate(15)
        # Only multiples of 3 become "Fizz"; no "Buzz" or "FizzBuzz"
        assert result[2] == "Fizz"
        assert result[4] == 5  # no longer "Buzz"
        assert result[14] == "Fizz"  # 15 % 3 == 0 still
        assert all("Buzz" not in str(v) for v in result)

    def test_remove_nonexistent(self):
        """remove_rule on non-existent divisor raises KeyError."""
        fb = FizzBuzz()
        with pytest.raises(KeyError):
            fb.remove_rule(99)

    def test_add_rule_invalid(self):
        """add_rule with divisor < 1 raises ValueError."""
        fb = FizzBuzz()
        with pytest.raises(ValueError, match="divisor must be a positive integer"):
            fb.add_rule(0, "Bad")

    def test_generate_matches_basic(self):
        """FizzBuzz().generate() matches fizzbuzz() for standard rules."""
        fb = FizzBuzz()
        for n in [1, 3, 5, 10, 15, 50]:
            assert fb.generate(n) == fizzbuzz(n)

    def test_multiple_rules_class(self):
        """Class with multiple rules produces concatenated output."""
        fb = FizzBuzz({2: "A", 3: "B", 5: "C"})
        result = fb.generate(30)
        # 30 -> A+B+C -> "ABC"
        assert result[29] == "ABC"

    def test_invalid_rules_type(self):
        """Non-dict rules raises TypeError."""
        with pytest.raises(TypeError, match="rules must be a dict"):
            FizzBuzz("not_a_dict")

    def test_negative_n(self):
        """generate() with n < 1 raises ValueError."""
        fb = FizzBuzz()
        with pytest.raises(ValueError, match="at least 1"):
            fb.generate(0)

    def test_empty_rules_after_remove(self):
        """Removing all rules still works (no matches, all numbers)."""
        fb = FizzBuzz({3: "Fizz"})
        fb.remove_rule(3)
        assert fb.generate(5) == [1, 2, 3, 4, 5]

    def test_repr_style_no_default(self):
        """Generate without default_output returns ints for unmatched."""
        fb = FizzBuzz({7: "Seven"})
        result = fb.generate(10)
        # Only 7 matches
        assert result[6] == "Seven"
        assert all(isinstance(v, int) for i, v in enumerate(result, 1) if i != 7)