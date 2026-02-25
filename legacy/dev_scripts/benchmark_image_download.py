import asyncio
import io
import time
from typing import List
from PIL import Image


# Mock classes to simulate Telegram objects
class MockFile:
    async def download_as_bytearray(self):
        await asyncio.sleep(0.2)  # Simulate download latency
        # Return a small valid image to satisfy Image.open
        img = Image.new("RGB", (10, 10), color="red")
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format="PNG")
        return img_byte_arr.getvalue()


class MockPhotoSize:
    async def get_file(self):
        await asyncio.sleep(0.05)  # Simulate API call latency
        return MockFile()


class MockMessage:
    def __init__(self):
        self.photo = [MockPhotoSize()]


async def sequential_download(messages: List[MockMessage]):
    images = []
    start_time = time.time()
    for i, message in enumerate(messages):
        try:
            photo_file = await message.photo[-1].get_file()
            photo_data = await photo_file.download_as_bytearray()
            img = Image.open(io.BytesIO(photo_data))
            images.append(img)
            # logging.info(f"📸 Загружено изображение {i+1}/{len(messages)}")
        except Exception as e:
            print(f"Error loading image {i + 1}: {e}")
            continue
    end_time = time.time()
    return end_time - start_time


async def concurrent_download(messages: List[MockMessage]):
    images = []
    start_time = time.time()

    async def process_message(message):
        photo_file = await message.photo[-1].get_file()
        photo_data = await photo_file.download_as_bytearray()
        return Image.open(io.BytesIO(photo_data))

    try:
        results = await asyncio.gather(
            *(process_message(msg) for msg in messages), return_exceptions=True
        )
        for res in results:
            if isinstance(res, Exception):
                print(f"Error loading image: {res}")
            else:
                images.append(res)
    except Exception as e:
        print(f"Error in concurrent download: {e}")

    end_time = time.time()
    return end_time - start_time


async def main():
    num_messages = 5
    messages = [MockMessage() for _ in range(num_messages)]

    print(f"Benchmarking with {num_messages} images...")

    sequential_time = await sequential_download(messages)
    print(f"Sequential time: {sequential_time:.4f} seconds")

    concurrent_time = await concurrent_download(messages)
    print(f"Concurrent time: {concurrent_time:.4f} seconds")

    improvement = (sequential_time - concurrent_time) / sequential_time * 100
    print(f"Improvement: {improvement:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
