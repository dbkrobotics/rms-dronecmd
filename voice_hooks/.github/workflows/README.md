# GitHub Actions Setup

## NPM Publishing Workflow

The `publish.yml` workflow automatically publishes the package to npm when you push a version tag.

### Setup Instructions

1. **Generate an NPM Access Token:**
   - Go to <https://www.npmjs.com/>
   - Sign in to your account
   - Click on your profile picture → Access Tokens
   - Click "Generate New Token"
   - Select "Classic Token"
   - Choose "Automation" type (for CI/CD)
   - Copy the token (it starts with `npm_`)

2. **Add the Token to GitHub Secrets:**
   - Go to your GitHub repository
   - Navigate to Settings → Secrets and variables → Actions
   - Click "New repository secret"
   - Name: `NPM_TOKEN`
   - Value: Paste your npm token
   - Click "Add secret"

3. **Publishing Process:**
   - The workflow triggers automatically when you push a tag starting with 'v'
   - Use `npm version patch/minor/major` locally to create a version tag
   - Push the tag: `git push origin --tags`
   - The GitHub Action will build and publish to npm

### Manual Publishing

If you prefer to publish manually:

```bash
# Build first
npm run build

# Bump version and create tag
npm version patch --registry https://registry.npmjs.org/

# Publish to npm
npm publish --registry https://registry.npmjs.org/
```
