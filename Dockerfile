FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Mount PARAFORM_STORAGE_STATE as a secret file at runtime; never bake it in.
CMD ["python", "-m", "app.main"]
