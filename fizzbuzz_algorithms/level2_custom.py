"""Level 2: Customizable FizzBuzz implementation.

Extends the basic FizzBuzz with a user-supplied rules dictionary,
allowing arbitrary divisor-to-word mappings.
"""

from typing import Dict, List, Union


def fizzbuzz_with_rules(
    n: int,
    rules: Dict[int, str],
) -> List[Union[int, str]]:
    """Return a FizzBuzz sequence with custom divisor→word mapping.

    For each number i from 1 to n, the output is the concatenation
    of words whose divisor divides i (sorted by divisor ascending).
    If no divisor matches, the number itself is used.

    Args:
        n: Upper bound (must be >= 1).
        rules: Mapping of divisor -> word, e.g. {3: "Fizz", 5: "Buzz"}.
               Divisors must be positive integers.

    Returns:
        A list of strings and ints representing the FizzBuzz sequence.

    Raises:
        ValueError: If n < 1 or any divisor < 1.
        TypeError: If n is not an integer or rules is not a dict.
    """
    if not isinstance(n, int):
        raise TypeError("n must be an integer")
    if n < 1:
        raise ValueError("n must be at least 1")
    if not isinstance(rules, dict):
        raise TypeError("rules must be a dict")
    if not rules:
        raise ValueError("rules must not be empty")

    # Validate and sort divisors for deterministic ordering
    sorted_divisors = sorted(rules.keys())
    for d in sorted_divisors:
        if not isinstance(d, int) or d < 1:
            raise ValueError(f"divisor must be a positive integer, got {d!r}")

    result: List[Union[int, str]] = []
    for i in range(1, n + 1):
        tokens = []
        for divisor in sorted_divisors:
            if i % divisor == 0:
                tokens.append(rules[divisor])
        if tokens:
            result.append("".join(tokens))
        else:
            result.append(i)
    return result