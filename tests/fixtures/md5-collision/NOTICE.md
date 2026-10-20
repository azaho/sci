# MD5 collision pair

`md5-1.zip` and `md5-2.zip` are two different 512-byte ZIP files with the same
MD5 digest (`2b980a3708ff9edfdd6c8dfbb42e4f8d`). They come unchanged from Ange
Albertini's "Tiny, copyright-free, PII-free collision PoCs":
https://github.com/corkami/collisions/tree/master/examples/free

| File | SHA-256 |
| --- | --- |
| `md5-1.zip` | `ef47a1e68e1fa539acfc06aaa0e8b0022cb5f2c53fcb4b971b0a99c7d9febfc4` |
| `md5-2.zip` | `afcd8f28d9fb01b02a9cccb2e1fcd1908bd58bb65ec07e7d3c6773fac61a24ac` |

`tests/test_security_copy_integrity.py` uses them to show that an ETag match is
not a byte match (SCI-SEC-003).
