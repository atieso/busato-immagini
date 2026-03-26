from shopify_ftp_sync_app import sync_images

result = sync_images()
print(result.model_dump_json(indent=2))
