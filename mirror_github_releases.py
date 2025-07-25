import os
import json
import requests
from github import Github
from github.GithubException import GithubException
import subprocess

# 配置参数
BATCH_SIZE = 10  # 每同步10个版本保存一次
RECORD_FILE = "synced_versions.json"  # 同步记录文件


def initialize_record_file():
    """确保记录文件存在，首次运行自动创建"""
    if not os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        print(f"初始化记录文件: {RECORD_FILE}")
    # 确保文件被Git跟踪（首次运行时）
    try:
        subprocess.run(["git", "add", RECORD_FILE], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print(f"警告: 无法将 {RECORD_FILE} 添加到Git跟踪")


def load_synced_versions():
    """加载已同步版本记录，确保文件存在"""
    initialize_record_file()  # 确保文件存在
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
    # 先写入临时文件，避免原文件损坏
    temp_file = f"{RECORD_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(synced_list, f, indent=2, ensure_ascii=False)
    # 原子操作替换原文件
    os.replace(temp_file, RECORD_FILE)
    print(f"已保存 {len(synced_list)} 条同步记录")


def git_commit_and_push(message):
    """提交记录文件，容错处理"""
    try:
        # 配置仓库用户信息
        subprocess.run(["git", "config", "user.email", "action@github.com"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "GitHub Action"], check=True, capture_output=True)
        
        # 检查文件是否有变化
        status = subprocess.run(["git", "status", "--porcelain", RECORD_FILE], capture_output=True, text=True).stdout
        if not status:
            print("记录文件无变化，无需提交")
            return True
        
        # 提交并推送
        subprocess.run(["git", "add", RECORD_FILE], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], check=True, capture_output=True)
        subprocess.run(["git", "push"], check=True, capture_output=True)
        print(f"✅ 成功提交: {message}")
        return True
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode().strip()
        print(f"⚠️ 提交失败: {error_msg}")
        return False


def sync_single_release(source_release, target_repo):
    """同步单个Release，包含资产文件"""
    # 检查目标仓库是否已存在该版本
    try:
        target_repo.get_release(source_release.tag_name)
        print(f"⚠️ {source_release.tag_name} 已存在，跳过")
        return False
    except GithubException:
        pass

    try:
        # 创建Release（自动创建标签）
        target_release = target_repo.create_git_release(
            tag=source_release.tag_name,
            name=source_release.title,
            message=source_release.body,
            draft=source_release.draft,
            prerelease=source_release.prerelease
        )
        print(f"📌 创建Release: {source_release.tag_name}")

        # 上传资产
        for asset in source_release.get_assets():
            temp_file = f"temp_{asset.id}_{asset.name}"
            try:
                # 流式下载资产
                with open(temp_file, "wb") as f:
                    asset.download_stream().readinto(f)
                
                # 上传到目标仓库
                target_release.upload_asset(
                    path=temp_file,
                    name=asset.name,
                    content_type=asset.content_type
                )
                print(f"✅ 上传成功: {asset.name}")
            finally:
                # 确保临时文件被删除
                if os.path.exists(temp_file):
                    os.remove(temp_file)

        return True
    except Exception as e:
        print(f"❌ 同步失败 {source_release.tag_name}: {str(e)}")
        return False


def main():
    # 加载环境变量
    source_repo_name = os.getenv("SOURCE_REPO")
    target_repo_name = os.getenv("TARGET_REPO") or os.getenv("GITHUB_REPOSITORY")
    source_token = os.getenv("SOURCE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    target_token = os.getenv("GITHUB_TOKEN")

    # 验证必要参数
    if not source_repo_name:
        print("错误: 未设置SOURCE_REPO环境变量")
        exit(1)

    # 初始化GitHub客户端
    try:
        source_gh = Github(source_token)
        target_gh = Github(target_token)
        source_repo = source_gh.get_repo(source_repo_name)
        target_repo = target_gh.get_repo(target_repo_name)
    except GithubException as e:
        print(f"错误: 仓库访问失败 - {str(e)}")
        exit(1)

    # 加载已同步记录（确保文件存在）
    synced_versions = load_synced_versions()
    print(f"📊 已同步版本: {len(synced_versions)} 个")

    # 获取源仓库Release（按创建时间正序）
    all_releases = list(source_repo.get_releases())
    all_releases.sort(key=lambda r: r.created_at)
    print(f"📥 源仓库共 {len(all_releases)} 个版本")

    # 筛选待同步版本（排除已同步和草稿）
    to_sync = [
        r for r in all_releases
        if r.tag_name not in synced_versions
        and not r.draft
    ]
    print(f"📋 待同步版本: {len(to_sync)} 个")

    if not to_sync:
        print("✅ 所有版本已同步")
        return

    # 同步计数器
    current_batch = 0
    last_saved_count = len(synced_versions)

    try:
        for release in to_sync:
            # 同步单个版本
            if sync_single_release(release, target_repo):
                synced_versions.append(release.tag_name)
                current_batch += 1

                # 每10个版本保存并提交
                if current_batch >= BATCH_SIZE:
                    save_synced_versions(synced_versions)
                    commit_msg = f"同步第 {last_saved_count + 1}-{len(synced_versions)} 个版本"
                    if git_commit_and_push(commit_msg):
                        last_saved_count = len(synced_versions)
                        current_batch = 0  # 重置计数器

    except Exception as e:
        print(f"⚠️ 同步过程中断: {str(e)}")
    finally:
        # 确保最后未提交的记录被保存
        if len(synced_versions) > last_saved_count:
            save_synced_versions(synced_versions)
            commit_msg = f"同步中断，已保存至 {len(synced_versions)} 个版本"
            git_commit_and_push(commit_msg)

    print("🎉 同步任务结束")


if __name__ == "__main__":
    main()
