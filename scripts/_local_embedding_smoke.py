import asyncio
import math

from dotenv import load_dotenv

from backend.app.core.llm_provider import create_embedding_client


async def main() -> None:
    load_dotenv()
    client = create_embedding_client()
    print(f"client={type(client).__name__}")
    print(f"model={client.model_name}")
    vectors = await client.embed(
        [
            "occupational exposure and lung cancer",
            "preoperative antibiotic prophylaxis",
        ]
    )
    print(f"count={len(vectors)}")
    print(f"dimensions={[len(vector) for vector in vectors]}")
    print(f"finite={all(all(math.isfinite(value) for value in vector) for vector in vectors)}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
