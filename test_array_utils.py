from array_utils import rotate_array, rotate_array_left

arr = [1, 2, 3, 4, 5]

# 测试向右旋转
assert rotate_array(arr, 2) == [4, 5, 1, 2, 3], f"rotate_array(arr, 2) failed: {rotate_array(arr, 2)}"
print("rotate_array(arr, 2) =", rotate_array(arr, 2))  # 期望 [4, 5, 1, 2, 3]

# 测试向左旋转
assert rotate_array_left(arr, 2) == [3, 4, 5, 1, 2], f"rotate_array_left(arr, 2) failed: {rotate_array_left(arr, 2)}"
print("rotate_array_left(arr, 2) =", rotate_array_left(arr, 2))  # 期望 [3, 4, 5, 1, 2]

# 测试空数组
assert rotate_array([], 2) == []
print("rotate_array([], 2) = []")

assert rotate_array_left([], 2) == []
print("rotate_array_left([], 2) = []")

# 测试 k > n 的情况
assert rotate_array(arr, 7) == [4, 5, 1, 2, 3], f"rotate_array(arr, 7) failed: {rotate_array(arr, 7)}"
print("rotate_array(arr, 7) =", rotate_array(arr, 7))  # 期望 [4, 5, 1, 2, 3]

# 测试 k = 0
assert rotate_array(arr, 0) == arr
assert rotate_array_left(arr, 0) == arr
print("rotate_array(arr, 0) =", arr)
print("rotate_array_left(arr, 0) =", arr)

print("\n所有测试通过！")