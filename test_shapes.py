from shapes import rectangle_area, rectangle_perimeter, Rectangle

# 测试函数
assert rectangle_area(5, 3) == 15, f"Expected 15, got {rectangle_area(5, 3)}"
assert rectangle_perimeter(5, 3) == 16, f"Expected 16, got {rectangle_perimeter(5, 3)}"

# 测试类
r = Rectangle(5, 3)
assert r.area() == 15, f"Expected 15, got {r.area()}"
assert r.perimeter() == 16, f"Expected 16, got {r.perimeter()}"

print("所有测试通过！")
print(f"rectangle_area(5, 3) = {rectangle_area(5, 3)}")
print(f"rectangle_perimeter(5, 3) = {rectangle_perimeter(5, 3)}")
print(f"Rectangle(5,3).area() = {r.area()}")
print(f"Rectangle(5,3).perimeter() = {r.perimeter()}")