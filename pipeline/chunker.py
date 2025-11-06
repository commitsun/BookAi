import tiktoken

ENCODER = tiktoken.get_encoding("cl100k_base")

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50):
    tokens = ENCODER.encode(text)
    chunks = []
    start = 0

    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk = ENCODER.decode(tokens[start:end])
        chunks.append(chunk)
        start += chunk_size - overlap

    return chunks
