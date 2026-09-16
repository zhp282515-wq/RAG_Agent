import yaml
from yaml import FullLoader
from utils.path_tool import get_abs_path


def get_model_config(config_path: str = get_abs_path("config\\model.yml"), encoding: str = "utf-8"):
    """全量加载获取模型配置"""
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, FullLoader)

def get_agent_config(config_path: str = get_abs_path("config\\agent.yml"), encoding: str = "utf-8"):
    """全量加载获取agent配置"""
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, FullLoader)

def get_rag_config(config_path: str = get_abs_path("config\\rag.yml"), encoding: str = "utf-8"):
    """全量加载获取RAG配置"""
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, FullLoader)

def get_vector_store_config(config_path: str = get_abs_path("config\\vector_store.yml"), encoding: str = "utf-8"):
    """全量加载获取向量存储配置"""
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.load(f, FullLoader)

def get_resilience_config(config_path: str = get_abs_path("config\\resilience.yml"), encoding: str = "utf-8"):
    """全量加载获取失败处理(重试/熔断/降级)配置。

    刻意与上面四个 loader 不同:文件缺失/损坏一律给空 dict,而不是让异常在 import
    时炸穿整个项目。其余 loader 少一个文件是启动期就该暴露的配置事故,而 resilience
    的定位是「没有它也要能跑」的兜底层 —— 它自己缺失时静默回退内置默认值即可。
    不要为了「一致性」把它改回抛异常。
    """
    try:
        with open(config_path, "r", encoding=encoding) as f:
            return yaml.load(f, FullLoader) or {}
    except (OSError, yaml.YAMLError):
        return {}


model_conf = get_model_config()
agent_conf = get_agent_config()
rag_conf = get_rag_config()
vector_store_conf = get_vector_store_config()
resilience_conf = get_resilience_config()



if __name__ == '__main__':
    print(model_conf["model"])