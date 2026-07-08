"""Level 1: Basic FizzBuzz implementation.

Provides a simple fizzbuzz(n) function that returns the classic
FizzBuzz sequence from 1 to n.
"""


def fizzbuzz(n: int):
    """Return a FizzBuzz sequence from 1 to n (inclusive).

    Rules:
    - Multiples of 3 -> "Fizz"
    - Multiples of 5 -> "Buzz"
    - Multiples of both 3 and 5 -> "FizzBuzz"
    - Otherwise -> the number itself

    Args:
        n: Upper bound (must be >= 1).

    Returns:
        A list of strings and ints representing the FizzBuzz sequence.

    Raises:
        ValueError: If n < 1.
        TypeError: If n is not an integer.
    """
    if not isinstance(n, int):
        raise TypeError("n must be an integer")
    if n < 1:
        raise ValueError("n must be at least 1")

    result = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            result.append("FizzBuzz")
        elif i % 3 == 0:
            result.append("Fizz")
        elif i % 5 == 0:
            result.append("Buzz")
        else:
            result.append(i)
    return result