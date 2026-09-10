# Decisions

1. Hatchling used to build backend 

2. branding.yaml is a standalone file but the config/report.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty (can be discussed)

3. common/schema.py implemented, some helper classes were made like Pose, Point2D to make it easier to structure data

# Questions to raise

1. branding.yaml is a standalone file but the config.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty but is this the way to go
2. raise question as to how it is expected the data is fed
3. questions regarding objects , the one shown over there. for now I have given each a deterministic shape but if they are dynamic schema.py must be adjusted
