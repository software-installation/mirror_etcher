import os
import json
import subprocess
import requests  # 新增：使用requests库下载文件，替代PyGithub的流式下载
from datetime import datetime
from github import Github
from github.GithubException import GithubException

# 配置参数
BATCH_SIZE = 10  # 每同步10个版本保存一次
RECORD_FILE = "synced_versions.json"  # 同步记录文件
TIME_DELTA_THRESHOLD = 1  # 时间差阈值（秒）
TIMEOUT = 300  # 下载超时时间（秒）


def initialize_record_file():
    """确保记录文件存在，首次运行自动创建"""
    if not os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        print(f"初始化记录文件: {RECORD_FILE}")
    # 确保文件被Git跟踪
    try:
        subprocess.run(["git", "add", RECORD_FILE], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print(f"警告: 无法将 {RECORD_FILE} 添加到Git跟踪")


def load_synced_versions():
    """加载已同步版本记录"""
    initialize_record_file()
    try:
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"错误: {RECORD_FILE} 格式损坏，将重置记录")
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        return []


def save_synced_versions(synced_list):
    """安全保存同步记录"""
    temp_file = f"{RECORD_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(synced_list, f, indent=2, ensure_ascii=False)
    os.replace(temp_file, RECORD_FILE)
    print(f"已保存 {len(synced_list)} 条同步记录")


def git_commit_and_push(message):
    """提交记录文件，容错处理"""
    try:
        subprocess.run(["git", "config", "user.email", "action@github.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "GitHub Action"], check=True, capture_output=True)
        
        status = subprocess.run(["git", "status", "--porcelain", RECORD_FILE], capture_output=True, text=True).stdout
        if not status:
            print("记录文件无变化，无需提交")
            return True
        
        subprocess.run(["git", "add", RECORD_FILE], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], check=True, capture_output=True)
        subprocess.run(["git", "push"], check=True, capture_output=True)
        print(f"✅ 成功提交: {message}")
        return True
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode().strip()
        print(f"⚠️ 提交失败: {error_msg}")
        return False


def get_asset_info(assets):
    """提取资产信息为字典（文件名: (大小, 更新时间)）"""
    return {
        asset.name: (asset.size, asset.updated_at) 
        for asset in assets
    }


def download_asset(asset, save_path, token=None):
    """使用requests下载资产文件，兼容所有PyGithub版本"""
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    
    try:
        # 发送GET请求下载文件
        response = requests.get(
            asset.browser_download_url,
            headers=headers,
            stream=True,
            timeout=TIMEOUT
        )
        response.raise_for_status()  # 检查HTTP错误
        
        # 写入文件
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:  # 过滤空块
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"下载失败 {asset.name}: {str(e)}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False


def sync_release_assets(source_release, target_release, source_token):
    """同步版本中的资产文件（使用requests下载）"""
    # 获取源和目标的资产信息
    source_assets = source_release.get_assets()
    target_assets = target_release.get_assets()
    
    source_info = get_asset_info(source_assets)
    target_info = get_asset_info(target_assets)
    
    # 筛选需要上传的资产
    to_upload = []
    for asset in source_assets:
        name = asset.name
        if name not in target_info:
            to_upload.append(asset)
            print(f"需要上传: {name}（目标不存在）")
        else:
            target_size, target_time = target_info[name]
            if asset.size != target_size:
                to_upload.append(asset)
                print(f"需要上传: {name}（大小不匹配，源:{asset.size}，目标:{target_size}）")
            else:
                time_diff = (asset.updated_at - target_time).total_seconds()
                if time_diff > TIME_DELTA_THRESHOLD:
                    to_upload.append(asset)
                    print(f"需要上传: {name}（源文件更新，时间差:{time_diff:.1f}秒）")
    
    if not to_upload:
        print("所有资产文件已同步，无需更新")
        return True
    
    # 上传需要更新的资产
    for asset in to_upload:
        temp_file = f"temp_{asset.id}_{asset.name}"
        try:
            # 使用requests下载（替代原有的download_stream）
            if not download_asset(asset, temp_file, source_token):
                print(f"❌ 跳过 {asset.name} 因为下载失败")
                continue
            
            # 若目标已存在该文件，先删除旧版本
            if asset.name in target_info:
                for target_asset in target_assets:
                    if target_asset.name == asset.name:
                        target_asset.delete_asset()
                        print(f"已删除旧版本: {asset.name}")
                        break
            
            # 上传新文件
            target_release.upload_asset(
                path=temp_file,
                name=asset.name,
                content_type=asset.content_type
            )
            print(f"✅ 上传完成: {asset.name}（更新于 {asset.updated_at}）")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)
    
    return True


def sync_single_release(source_release, target_repo, source_token):
    """同步单个Release（使用兼容的下载方式）"""
    tag_name = source_release.tag_name
    
    try:
        # 检查目标版本是否存在
        target_release = target_repo.get_release(tag_name)
        print(f"版本 {tag_name} 已存在，检查资产更新...")
        
        # 验证并同步资产文件
        return sync_release_assets(source_release, target_release, source_token)
        
    except GithubException:
        # 版本不存在，创建并同步所有资产
        print(f"版本 {tag_name} 不存在，创建并同步资产...")
        target_release = target_repo.create_git_release(
            tag=tag_name,
            name=source_release.title,
            message=source_release.body,
            draft=source_release.draft,
            prerelease=source_release.prerelease
        )
        return sync_release_assets(source_release, target_release, source_token)


def main():
    # 环境变量
    source_repo_name = os.getenv("SOURCE_REPO")
    target_repo_name = os.getenv("TARGET_REPO") or os.getenv("GITHUB_REPOSITORY")
    source_token = os.getenv("SOURCE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    target_token = os.getenv("GITHUB_TOKEN")

    if not source_repo_name:
        print("错误: 未设置SOURCE_REPO环境变量")
        exit(1)

    # 初始化客户端
    try:
        source_gh = Github(source_token)
        target_gh = Github(target_token)
        source_repo = source_gh.get_repo(source_repo_name)
        target_repo = target_gh.get_repo(target_repo_name)
    except GithubException as e:
        print(f"错误: 仓库访问失败 - {str(e)}")
        exit(1)

    # 加载记录
    synced_versions = load_synced_versions()
    print(f"📊 已同步版本: {len(synced_versions)} 个")

    # 获取源版本（按创建时间正序）
    all_releases = list(source_repo.get_releases())
    all_releases.sort(key=lambda r: r.created_at)
    print(f"📥 源仓库共 {len(all_releases)} 个版本")

    # 待同步版本（排除草稿）
    to_sync = [r for r in all_releases if not r.draft]
    print(f"📋 待检查版本: {len(to_sync)} 个")

    # 同步计数器
    current_batch = 0
    last_saved_count = len(synced_versions)

    try:
        for release in to_sync:
            # 同步单个版本，传入source_token用于下载
            if sync_single_release(release, target_repo, source_token):
                # 若版本是首次同步，加入记录
                if release.tag_name not in synced_versions:
                    synced_versions.append(release.tag_name)
                    current_batch += 1

                # 每10个版本保存一次
                if current_batch >= BATCH_SIZE:
                    save_synced_versions(synced_versions)
                    commit_msg = f"同步第 {last_saved_count + 1}-{len(synced_versions)} 个版本"
                    if git_commit_and_push(commit_msg):
                        last_saved_count = len(synced_versions)
                        current_batch = 0

    except Exception as e:
        print(f"⚠️ 同步中断: {str(e)}")
    finally:
        # 保存最后记录
        if len(synced_versions) > last_saved_count:
            save_synced_versions(synced_versions)
            git_commit_and_push(f"同步中断，已保存至 {len(synced_versions)} 个版本")

    print("🎉 同步任务结束")


if __name__ == "__main__":
    main()
