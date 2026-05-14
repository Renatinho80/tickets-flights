# Dockerfile otimizado para Render.com (Web Service)
# Utilizamos a imagem oficial do Playwright para garantir que todas as dependências 
# do sistema operacional necessárias para rodar o Chromium estejam pré-instaladas.
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Variáveis de ambiente de otimização do Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Define o diretório de trabalho dentro do container
WORKDIR /app

# Instala ferramentas básicas de build, caso o pacote pyiceberg ou pandas precisem compilar algo C++
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copia o arquivo de dependências e o README (necessário para o metadata do pacote)
COPY pyproject.toml README.md ./

# Instala as dependências do Python
RUN pip install --upgrade pip
RUN pip install -e ".[all]"

# O Playwright do Python ainda precisa baixar os binários específicos dessa versão
RUN playwright install chromium

# Copia o resto do código do projeto para dentro do container
COPY . .

# Expõe a porta que o Render vai ler
EXPOSE 8000

# Comando para iniciar a API no Render
CMD ["uvicorn", "flight_analyst.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
