import pathlib

p = pathlib.Path("app/web.py")
raw = p.read_bytes()

# The routes to append:
ROUTES = b'''
# \xe2\x94\x80\xe2\x94\x80 Admin Daily Crocodile Dashboard \xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80

@quart_app.route("/admin_dailycroc")
@require_auth
async def admin_dailycroc_page():
    """Serve the Daily Crocodile Admin Dashboard."""
    return await render_template("admin_dailycroc.html")


@quart_app.route("/api/admin/dailycroc", methods=["GET"])
@require_auth
async def api_admin_dailycroc_list():
    from app.repos.crocodile_daily import get_recent_daily_puzzles
    from app.config import settings
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    puzzles = await get_recent_daily_puzzles(limit=limit)
    out = []
    for puzzle in puzzles:
        if puzzle.puzzle_date is None:
            continue
        out.append({
            "date": puzzle.puzzle_date.isoformat(),
            "difficulty": puzzle.difficulty,
            "target_word": puzzle.target_word,
            "topic": puzzle.topic,
            "image_file_id": puzzle.image_file_id,
            "image_prompt": puzzle.image_prompt,
            "image_model": puzzle.image_model,
        })
    return jsonify({"puzzles": out})


@quart_app.route("/api/admin/dailycroc/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailycroc_regen():
    from app.repos.crocodile_daily import get_daily_puzzle_strict, set_puzzle_image_asset
    from app.providers.pollinations import generate_image_model
    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    if not puzzle_date or not difficulty:
        return jsonify({"error": "missing date or difficulty"}), 400
    import datetime
    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date format"}), 400
    
    puzzle = await get_daily_puzzle_strict(dt, difficulty)
    if not puzzle:
        return jsonify({"error": "puzzle not found"}), 404
        
    try:
        from app.bot_instance import get_bot
        from telegram import InputMediaPhoto
        bot = get_bot()
        if bot is None:
            return jsonify({"error": "bot not ready"}), 503
            
        model = puzzle.image_model or "zimage"
        photo_bytes = await generate_image_model(puzzle.image_prompt, width=1024, height=1024, model=model)
        
        # Send to config group to get file_id
        from app.config import settings
        msg = await bot.send_photo(chat_id=settings.CONFIG_CHAT_ID, photo=photo_bytes)
        file_id = msg.photo[-1].file_id
        
        await set_puzzle_image_asset(dt, file_id, difficulty=difficulty)
        return jsonify({"success": True, "file_id": file_id})
    except Exception as e:
        logging.error("Regen failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@quart_app.route("/api/admin/dailycroc/prompt", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_prompt():
    from app.repos.crocodile_daily import update_daily_puzzle_prompt
    data = await request.get_json()
    if not data: return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    prompt = data.get("prompt")
    if not puzzle_date or not difficulty or prompt is None:
        return jsonify({"error": "missing fields"}), 400
    import datetime
    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    
    await update_daily_puzzle_prompt(dt, difficulty, prompt)
    return jsonify({"success": True})


@quart_app.route("/api/admin/dailycroc/model", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_model():
    data = await request.get_json()
    if not data: return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    model = data.get("model")
    if not puzzle_date or not difficulty or model is None:
        return jsonify({"error": "missing fields"}), 400
    import datetime
    from app.database import db_manager
    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
        
    async with db_manager.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.crocodile_daily_puzzles
            SET image_model = $1
            WHERE puzzle_date = $2 AND difficulty = $3
            """,
            model, dt, difficulty
        )
    return jsonify({"success": True})


@quart_app.route("/api/admin/dailycroc/reset-word", methods=["POST"])
@require_auth
async def api_admin_dailycroc_reset_word():
    from app.repos.crocodile_daily import regenerate_puzzle_word
    data = await request.get_json()
    if not data: return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    if not puzzle_date or not difficulty:
        return jsonify({"error": "missing date or difficulty"}), 400
    import datetime
    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date format"}), 400
        
    new_puzzle = await regenerate_puzzle_word(dt, difficulty)
    if not new_puzzle:
        return jsonify({"error": "failed to regenerate puzzle"}), 500
        
    return jsonify({
        "success": True,
        "new_word": new_puzzle.target_word,
        "new_topic": new_puzzle.topic,
    })


@quart_app.route("/api/admin/dailycroc/image", methods=["GET"])
@require_auth
async def api_admin_dailycroc_image():
    """Proxy a Telegram file_id as raw image bytes for dashboard preview."""
    file_id = request.args.get("file_id", "")
    if not file_id:
        return jsonify({"error": "missing file_id"}), 400

    try:
        import io
        from quart import Response
        from app.utils.tg_file import get_file_bytes
        from app.bot_instance import get_bot
        
        bot = get_bot()
        if bot is None:
            return jsonify({"error": "bot_not_ready"}), 503
            
        tg_file = await bot.get_file(file_id)
        data = await get_file_bytes(bot, tg_file)
        return Response(
            io.BytesIO(data).read(),
            status=200,
            headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": "public, max-age=3600",
            },
        )
    except Exception as exc:
        logging.error("Admin image proxy failed file_id=%s: %s", file_id, exc, exc_info=True)
        return jsonify({"error": "proxy_error", "detail": str(exc)}), 502
'''

p.write_bytes(raw + b"\n" + ROUTES)
