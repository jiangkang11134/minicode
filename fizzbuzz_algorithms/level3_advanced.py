"""Level 3: FizzBuzz class with configurable rules.

Provides a FizzBuzz class that supports configuration via a rules dict,
default output, and range generation.
"""

from typing import Dict, List, Optional, Union


class FizzBuzz:
    """A configurable FizzBuzz generator.

    The class stores a rules dictionary mapping divisors to words,
    and provides a generate() method to produce sequences.

    Attributes:
        rules: Mapping of divisor -> word (inherited or configured).
        default_output: Fallback value when no divisor matches.

    Examples:
        >>> fb = FizzBuzz({3: "Fizz", 5: "Buzz"})
        >>> fb.generate(5)
        [1, 2, 'Fizz', 4, 'Buzz']
    """

    def __init__(
        self,
        rules: Optional[Dict[int, str]] = None,
        default_output: Optional[str] = None,
    ) -> None:
        """Initialize the FizzBuzz generator.

        Args:
            rules: Mapping of divisor -> word. Defaults to {3: "Fizz", 5: "Buzz"}.
            default_output: When set, use this string instead of the number
                            when no divisor matches. If None, the number is used.

        Raises:
            TypeError: If rules is not a dict.
            ValueError: If any divisor is not a positive integer.
        """
        if rules is None:
            rules = {3: "Fizz", 5: "Buzz"}
        if not isinstance(rules, dict):
            raise TypeError("rules must be a dict")

        self.rules = rules
        self.default_output = default_output
        self._sorted_divisors = sorted(rules.keys())

        for d in self._sorted_divisors:
            if not isinstance(d, int) or d < 1:
                raise ValueError(f"divisor must be a positive integer, got {d!r}")

    def generate(self, n: int) -> List[Union[int, str]]:
        """Generate a FizzBuzz sequence from 1 to n.

        Args:
            n: Upper bound (must be >= 1).

        Returns:
            A list of strings and ints.

        Raises:
            ValueError: If n < 1.
            TypeError: If n is not an integer.
        """
        if not isinstance(n, int):
            raise TypeError("n must be an integer")
        if n < 1:
            raise ValueError("n must be at least 1")

        result: List[Union[int, str]] = []
        for i in range(1, n + 1):
            tokens = []
            for divisor in self._sorted_divisors:
                if i % divisor == 0:
                    tokens.append(self.rules[divisor])
            if tokens:
                result.append("".join(tokens))
            elif self.default_output is not None:
                result.append(self.default_output)
            else:
                result.append(i)
        return result

    def add_rule(self, divisor: int, word: str) -> None:
        """Add or update a divisor→word rule.

        Args:
            divisor: Positive integer divisor.
            word: Word to output when the divisor divides the number.

        Raises:
            ValueError: If divisor < 1.
        """
        if not isinstance(divisor, int) or divisor < 1:
            raise ValueError("divisor must be a positive integer")
        self.rules[divisor] = word
        self._sorted_divisors = sorted(self.rules.keys())

    def remove_rule(self, divisor: int) -> None:
        """Remove a rule by divisor.

        Args:
            divisor: The divisor to remove.

        Raises:
            KeyError: If the divisor does not exist.
        """
        del self.rules[divisor]
        self._sorted_divisors = sorted(self.rules.keys())