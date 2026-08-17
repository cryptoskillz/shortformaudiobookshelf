[ ] add a stciky footer for play on mobile
[ ] add a ai transcript toll that will take a full book and create you a 20 minute summary
[ ] add a a-z filter
[ ] add a recomendation section
[ ] add a use may also like to the book
[ ] scoped API key for the rescan webhook (ApiKeyStore class is written in server.py but NOT wired up — needs routes, the scope check on /api/rescan, and a Settings panel to create/revoke). Lets Bindery trigger a rescan after import without handing it an admin password.
[x] is slow on the first load due ot the size of the the json file we may want to move it to sqllite 
[ ] have a way to export it so something like Bindery could pull it in and have a 3rd option (ebook/audiobook/short-audiobook)
[x] package it up get it on github so we can deploy it (Portainer Repository stack from the public repo, env-var paths, docs rewritten)
[x] docs for docker install
[x] on import have the option to delete the original file
[x] add a user manager admin lets you scan / delete / add books etc 
[x] show author bio
[x] short list audio should take you to the home
[x] remove book (with delete option)
[x] add a scan on the book page
[x] show book description (get this from the same place you get the cover?)
[x] settings to set upload / download and audio directories etc (library + output done; upload dir pending #1)
[x] book page author name should be a hyperlink
[x] make it a docker container so it works on ugreen (Dockerfile + compose + paste-into-Portainer stack, tested)
[x] add a github so it does not include the covers or default meta info etc
