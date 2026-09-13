# Security policy

- Never commit `.env`, API keys or private scan reports.
- Start with `demo/sample_disk` or a small cache directory instead of the whole system drive.
- The default `balanced` privacy mode masks the OS username, but filenames may still contain sensitive information.
- Review generated reports before enabling `--trash-auto`.
- The public version refuses protected roots, whole-directory deletion and permanent deletion.
- Before moving a file to the recycle bin, the program rechecks size, modification time and file identity.
- AI benchmark mode is advisory only and is not connected to cleanup operations.
- Report security issues without including private paths, access tokens or personal documents.
