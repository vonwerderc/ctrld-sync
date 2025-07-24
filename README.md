# Control-D Sync

A tiny Python script that keeps a Control-D in sync with the latest Hagezi block-lists hosted on GitHub.

## What it does
1. Downloads the current JSON block-lists.
2. Deletes any existing folders with the same names.
3. Re-creates the folders and pushes all rules in batches.

## Quick start

1. **Clone & install**
   ```bash
   git clone https://github.com/your-username/ctrld-sync.git
   cd ctrld-sync
   uv sync
   ```

2. **Configure secrets**  
   Create a `.env` file (or set GitHub secrets) with:
   ```
   TOKEN=your_control_d_api_token
   PROFILE=your_profile_uuid
   ```

3. **Configure Folders**
   Edit the `FOLDER_URLS` list in `main.py` to include the URLs of the JSON block-lists you want to sync.

   > [!NOTE]
   > Currently only Folders with one action are supported.
   > Either "Block" or "Allow" actions are supported.

4. **Run locally**
   ```bash
   uv run python main.py
   ```

5. **Run in CI**  
   The included GitHub Actions workflow runs daily at 02:00 UTC and on demand.

## Requirements
- Python 3.12+  
- `uv` (for dependency management)