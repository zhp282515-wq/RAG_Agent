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


model_conf = get_model_config()
agent_conf = get_agent_config()
rag_conf = get_rag_config()
vector_store_conf = get_vector_store_config()



if __name__ == '__main__':
    print(model_conf["model"])