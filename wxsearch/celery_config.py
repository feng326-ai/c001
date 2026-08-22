"""
Celery Worker 配置文件
用于启动后台处理服务
"""

# Celery 配置
broker_url = 'redis://localhost:6379/0'
result_backend = 'redis://localhost:6379/1'

# 时区设置
timezone = 'Asia/Shanghai'

# 日期时间格式
enable_utc = True

# 任务并发数
worker_concurrency = 4

# 任务日志
worker_log_level = 'INFO'

# 自动重试
task_acks_late = True
task_reject_on_worker_lost = True
task_soft_failure = False

# 最大重试次数
task_max_retries = 3

# 重试等待时间 (秒)
task_default_retry_delay = 60
