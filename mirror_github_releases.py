import os
import json
import requests
from github import Github
from github.GithubException import GithubException
import subprocess

# 配置参数
BATCH_SIZE = 10  # 每同步10个版本保存一次记录
RECORD_FILE = "synced_versions.json"  # 记录已同步版本的文件


def load_synced_versions():
    """加载已同步的版本记录"""
    if not os.path.exists(RECORD_FILE):
        return []
    try:
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"警告：{RECORD_FILE} 格式错误，将重新创建记录文件")
        return []


def save_synced_versions(synced_list):
    """保存已同步的版本记录到文件"""
    with open(RECORD_FILE, "w", encoding="utf-8") as f:
        json.dump(synced_list, f, indent=2, ensure_ascii=False)
    print(f"已保存记录：共 {len(synced_list)} 个版本")


def git_commit_and_push(message):
    """提交记录文件到仓库"""
    try:
        subprocess.run(["git", "add", RECORD_FILE], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], check=True, capture_output=True)
        subprocess.run(["git", "push"], check=True, capture_output=True)
        print(f"✅ Git提交成功：{message}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Git提交失败：{str(e.stderr)}")
        # 提交失败不中断，继续同步（避免因网络问题导致整体失败）


def sync_single_release(source_release, target_repo):
    """同步单个Release（创建标签、Release和上传资产）"""
    # 检查目标仓库是否已存在该标签
    try:
        target_repo.get_release(source_release.tag_name)
        print(f"⚠️ 版本 {source_release.tag_name} 已存在，跳过")
        return False
    except GithubException:
        pass  # 版本不存在，继续同步

    try:
        # 创建Release（自动创建标签）
        target_release = target_repo.create_git_release(
            tag=source_release.tag_name,
            name=source_release.title,
            message=source_release.body,
            draft=source_release.draft,
            prerelease=source_release.prerelease
        )
        print(f"📌 创建Release：{source_release.tag_name}")

        # 上传所有资产文件
        for asset in source_release.get_assets():
            print(f"📤 开始上传：{asset.name}（{asset.size} bytes）")
            # 下载资产到临时文件
            temp_file = f"temp_{asset.id}_{asset.name}"
            with open(temp_file, "wb") as f:
                asset.download_stream().readinto(f)
            
            # 上传到目标Release
            target_release.upload_asset(
                path=temp_file,
                name=asset.name,
                content_type=asset.content_type
            )
            os.remove(temp_file)  # 清理临时文件
            print(f"✅ 上传完成：{asset.name}")

        return True
    except Exception as e:
        print(f"❌ 同步失败 {source_release.tag_name}：{str(e)}")
        return False


def main():
    # 获取环境变量
    source_repo_name = os.getenv("SOURCE_REPO")
    target_repo_name = os.getenv("TARGET_REPO") or os.getenv("GITHUB_REPOSITORY")
    source_token = os.getenv("SOURCE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    target_token = os.getenv("GITHUB_TOKEN")

    # 验证必要参数
    if not source_repo_name:
        print("错误：SOURCE_REPO环境变量未设置")
        exit(1)

    # 初始化GitHub客户端
    source_gh = Github(source_token)
    target_gh = Github(target_token)

    try:
        source_repo = source_gh.get_repo(source_repo_name)
        target_repo = target_gh.get_repo(target_repo_name)
    except GithubException as e:
        print(f"错误：获取仓库失败 - {str(e)}")
        exit(1)

    # 加载已同步记录
    synced_versions = load_synced_versions()
    print(f"📊 已同步版本：{len(synced_versions)} 个")

    # 获取源仓库所有Release（按创建时间正序排列，从旧到新同步）
    all_releases = list(source_repo.get_releases())
    all_releases.sort(key=lambda r: r.created_at)
    print(f"📥 源仓库总版本数：{len(all_releases)} 个")

    # 筛选未同步的版本
    to_sync = [
        release for release in all_releases
        if release.tag_name not in synced_versions
        and not release.draft  # 跳过草稿版本
    ]
    print(f"📋 待同步版本：{len(to_sync)} 个")

    if not to_sync:
        print("✅ 所有版本已同步，退出程序")
        return

    # 开始同步
    current_batch_count = 0  # 当前批次同步数量
    for release in to_sync:
        try:
            # 同步单个版本
            if sync_single_release(release, target_repo):
                synced_versions.append(release.tag_name)
                current_batch_count += 1

                # 每同步10个版本保存一次记录
                if current_batch_count >= BATCH_SIZE:
                    save_synced_versions(synced_versions)
                    git_commit_and_push(
                        f"程序1：同步 {current_batch_count} 个版本（累计 {len(synced_versions)} 个）"
                    )
                    current_batch_count = 0  # 重置批次计数器
        except Exception as e:
            print(f"⚠️ 处理 {release.tag_name} 时出错：{str(e)}，继续下一个版本")
            continue

    # 同步结束后，保存剩余记录（不足10个的部分）
    if current_batch_count > 0:
        save_synced_versions(synced_versions)
        git_commit_and_push(
            f"程序1：同步结束，新增 {current_batch_count} 个版本（累计 {len(synced_versions)} 个）"
        )

    print("🎉 同步任务完成")


if __name__ == "__main__":
    main()
