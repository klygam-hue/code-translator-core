def sum_squares(limit):
    total = 0
    for number in range(1, limit):
        if number == 3:
            continue
        total += number * number
    return total


print(sum_squares(5), -7 // 3, -7 % 3, True, sep=" | ")
