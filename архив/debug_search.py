# debug_search.py
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

DB_PATH = "data/vector_db"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

print("1. Подключаемся к ChromaDB...")
client = chromadb.PersistentClient(path=DB_PATH)

print("2. Список коллекций:", client.list_collections())

print("3. Получаем коллекцию БЕЗ embedding_function...")
col_raw = client.get_collection(name="tariff_docs")
print(f"   count = {col_raw.count()}")

print("\n4. Пробный query БЕЗ embedding_function (raw)...")
try:
    res = col_raw.query(query_texts=["тариф"], n_results=3)
    print(f"   Результатов: {len(res['documents'][0])}")
    for d, m in zip(res["documents"][0], res["metadatas"][0]):
        print(f"   -> {m.get('filename')} | {d[:80]}")
except Exception as e:
    print(f"   ОШИБКА: {e}")

print("\n5. Пробный query С multilingual embedding_function...")
try:
    emb = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    col_emb = client.get_collection(name="tariff_docs", embedding_function=emb)
    res2 = col_emb.query(query_texts=["тариф"], n_results=3)
    print(f"   Результатов: {len(res2['documents'][0])}")
    for d, m in zip(res2["documents"][0], res2["metadatas"][0]):
        print(f"   -> {m.get('filename')} | {d[:80]}")
except Exception as e:
    print(f"   ОШИБКА: {e}")