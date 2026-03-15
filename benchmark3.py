import datetime
import timeit

conversations = [
    {
        'id': i,
        'title': f'Conversation {i}',
        'role_title': f'Role {i}' if i % 2 == 0 else None,
        'created_at': datetime.datetime.now(),
        'token_budget': i * 100
    }
    for i in range(10)
]

def test_concat():
    text = f"📝 *Сохранённые беседы* (страница {1})\n\n"

    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv["created_at"].strftime("%d.%m.%Y %H:%M") if conv["created_at"] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created} | 💬 {conv['token_budget'] or 0} токенов\n\n"
    return text

def test_join_optimized():
    parts = [f"📝 *Сохранённые беседы* (страница {1})\n\n"]
    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv["created_at"].strftime("%d.%m.%Y %H:%M") if conv["created_at"] else "Неизвестно"
        parts.append(
            f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
            f"📅 {created} | 💬 {conv['token_budget'] or 0} токенов\n\n"
        )
    return "".join(parts)

print("Running benchmarks (1000000 iterations)...")
concat_time = timeit.timeit(test_concat, number=100000)
join_opt_time = timeit.timeit(test_join_optimized, number=100000)

print(f"Concat: {concat_time:.4f}s")
print(f"Join (Optimized single string): {join_opt_time:.4f}s")

# Ensure outputs match
assert test_concat() == test_join_optimized()
