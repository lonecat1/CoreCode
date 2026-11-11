def multiplication_table():
    for i in range(1, 10):
        row = []
        for j in range(1, i + 1):
            row.append(f"{j}x{i}={i * j:<2}")
        print(" ".join(row))


if __name__ == "__main__":
    multiplication_table()
