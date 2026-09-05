import os


def get_project_path() -> str:
    """获取项目根目录的绝对路径"""

    # 获取当前文件的绝对路径
    current_path = os.path.abspath(__file__)

    # 获取项目根目录的绝对路径
    project_path = os.path.dirname(os.path.dirname(current_path))

    return project_path


def get_abs_path(path: str) -> str:
    """根据文件相对路径获取绝对路径"""
    return os.path.abspath(os.path.join(get_project_path(), path.lstrip("\\/")))



if __name__ == '__main__':
    data_path = get_abs_path("data")
    print(data_path)