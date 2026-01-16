# 微信云托管 Flask 后端

目录结构：

- app.py：Flask 应用入口，暴露 `/api/*` 路由
- requirements.txt：后端依赖
- Dockerfile：云托管容器镜像构建文件
- .dockerignore：容器构建忽略

本地运行（可选）：

```bash
pip install -r requirements.txt
FLASK_APP=app.py flask run --host 0.0.0.0 --port 8080
```

部署到云托管：

1. 在控制台创建服务（服务名建议：`flask-qxh1`），端口 8080
2. 上传并构建镜像（Dockerfile 会使用 gunicorn 绑定 8080）
3. 发布版本，确认服务正常

MySQL 配置：

后端通过环境变量读取 MySQL 连接信息（云托管控制台“环境变量”配置）：

- `MYSQL_ADDRESS`：形如 `10.35.102.75:3306`（或使用 `MYSQL_HOST` + `MYSQL_PORT`）
- `MYSQL_USER` / `MYSQL_PASSWORD`
- `MYSQL_DATABASE`：默认 `food_analyze`

启动时会自动创建数据库与表（`users/meals/meal_items/food_items`）。

图片识别（百度菜品识别）：

在云托管服务的环境变量中配置：

- `BAIDU_API_KEY`
- `BAIDU_SECRET_KEY`

AI 分析（火山引擎）：

在云托管服务的环境变量中配置：

- `VOLC_API_KEY`：火山引擎 API Key
- `VOLC_MODEL`：模型 Endpoint ID（例如 `ep-20250116...`）
