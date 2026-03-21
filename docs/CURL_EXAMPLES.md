# cURL Examples

## Health Check

```bash
curl http://localhost:8000/
```

## Implement Ticket

```bash
curl -X POST http://localhost:8000/implementTicket \
  -H "Content-Type: application/json" \
  -d '{"ticket": "Your ticket content here"}'
```
