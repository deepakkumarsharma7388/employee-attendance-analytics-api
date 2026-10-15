# Employee Attendance & Analytics API

A FastAPI application for employee management, attendance tracking, and attendance analytics using MongoDB.

## Tech stack

- Python 3.11+
- FastAPI
- MongoDB
- PyMongo
- Uvicorn

## Requirements

- Python installed
- MongoDB 6.0 or newer, locally or through MongoDB Atlas

## Local setup

Create and activate a virtual environment in the project directory.

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` as environment variables for your MongoDB instance. Do not commit credentials or `.env` files.

Start the API:

```powershell
python -m uvicorn app.main:app --port 8000
```

Open the interactive API documentation at:

http://127.0.0.1:8000/docs

Health check:

http://127.0.0.1:8000/health

## Project structure

- `app/main.py` — API implementation
- `requirements.txt` — Python dependencies
- `openapi.yaml` — API contract
- `DATA_MODEL.md` — database model
- `REVIEW.md` — review notes and test observations
- `DECISIONS.md` — engineering decisions
- `sample_data/` — sample MongoDB documents
- `sample_seed.py` — sample data loading script

## Testing

The health check, employee creation, employee listing, punch-in, and attendance listing were tested locally through Swagger UI. Duplicate punch-out was also tested and returned HTTP 409.

Analytics and edge cases still require additional verification before claiming full compliance with all business rules.
