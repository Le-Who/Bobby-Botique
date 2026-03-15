import timeit

def string_concat(n):
    text = "Initial state\n"
    for i in range(n):
        text += f"Line {i}: value {i * 2}\n"
    return text

def string_join(n):
    parts = ["Initial state\n"]
    for i in range(n):
        parts.append(f"Line {i}: value {i * 2}\n")
    return "".join(parts)

n = 1000
concat_time = timeit.timeit(lambda: string_concat(n), number=1000)
join_time = timeit.timeit(lambda: string_join(n), number=1000)

print(f"Concat time: {concat_time:.4f}s")
print(f"Join time: {join_time:.4f}s")
print(f"Improvement: {((concat_time - join_time) / concat_time) * 100:.2f}%")
