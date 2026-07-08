"""Test for fibonacci function in S03.py"""
from S03 import fibonacci


def test_fibonacci_base():
    """Test base cases"""
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1


def test_fibonacci_small():
    """Test small values"""
    assert fibonacci(2) == 1
    assert fibonacci(3) == 2
    assert fibonacci(4) == 3
    assert fibonacci(5) == 5


def test_fibonacci_larger():
    """Test larger values"""
    assert fibonacci(10) == 55
    assert fibonacci(20) == 6765


def test_fibonacci_negative():
    """Test negative input raises error"""
    try:
        fibonacci(-1)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
