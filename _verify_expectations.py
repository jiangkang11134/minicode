import math

# Test default rules expectations
n = 100
assert n // 3 == 33
assert n // 5 == 20
assert n // 15 == 6
assert 100 - 33 - 20 + 6 == 53

n = 15
assert n // 3 == 5
assert n // 5 == 3
assert n // 15 == 1
assert 15 - 5 - 3 + 1 == 8

# Custom {2:'A',3:'B'}
n = 6
assert n // 2 == 3
assert n // 3 == 2
assert n // 6 == 1  # lcm = 6

print("All expectations verified ✓")