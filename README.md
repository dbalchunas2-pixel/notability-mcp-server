# Notability MCP Server

Custom MCP server that reads Notability PDF auto-backups from Google Drive.

## Setup

1. Enable Notability Auto-Backup to Google Drive (PDF format)
2. Create a Google Cloud service account with Drive API access
3. Share the Notability folder with the service account email
4. Deploy on Railway with env vars: `GOOGLE_SERVICE_ACCOUNT_JSON` and `GOOGLE_DRIVE_FOLDER_ID`
5. Add the Railway URL to Littlebird Settings > Integrations > Add Custom MCP
