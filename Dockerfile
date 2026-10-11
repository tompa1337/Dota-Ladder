# Kör sajten hos en värdtjänst (t.ex. Railway). Bara Python behövs, inga paket.
FROM python:3.13-slim
WORKDIR /app
COPY turbo_rank.py turbo_site.py ./
# Sajten lyssnar på port 8080. PYTHONUNBUFFERED gör att utskrifter syns direkt i loggen.
ENV TURBO_PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "turbo_site.py"]
