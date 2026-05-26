import asyncio

from app.crypto import encrypt_api_key
from app.repos.settings_repo import set_global_setting


async def main():
    horoscope_key = '0nwaLaeiePnOTzcvWLuSpcMFjK6kvML6Ubsb9LgI'
    enc_key = encrypt_api_key(horoscope_key)
    await set_global_setting('provider_key:horoscope', enc_key)
    print('Horoscope key seeded.')

asyncio.run(main())
