FROM python:3.10.5-slim

# Set working directory
WORKDIR /app

# Verify Python's built-in SQLite support; the sqlite3 CLI is not needed
RUN python -c "import sqlite3; sqlite3.connect(':memory:').execute('SELECT 1')"

# Copy Pipfile and Pipfile.lock
COPY Pipfile Pipfile.lock ./

# Install pipenv and dependencies
RUN pip install pipenv && \
    pipenv install --system --deploy

# Copy the rest of the application
COPY . .

# Create necessary directories
RUN mkdir -p /app/data

# Set the default database location
ENV DATABASE_PATH=/app/data/football.db

# Expose port (if needed for health checks)
EXPOSE 8080

# Run the bot
CMD ["python", "main.py"]
