"""FizzBuzz algorithm implementations — three levels of sophistication.

Available components:
    fizzbuzz              — basic FizzBuzz function (level1_basic)
    fizzbuzz_with_rules   — customizable rules function (level2_custom)
    FizzBuzz              — configurable class (level3_advanced)
"""
from fizzbuzz_algorithms.level1_basic import fizzbuzz
from fizzbuzz_algorithms.level2_custom import fizzbuzz_with_rules
from fizzbuzz_algorithms.level3_advanced import FizzBuzz

__all__ = ["fizzbuzz", "fizzbuzz_with_rules", "FizzBuzz"]