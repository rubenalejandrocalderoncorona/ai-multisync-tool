# Static binary, no OS layer. Build the binary first (the Dockerfile only packages it):
#   CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o multisync-webhook .   (run inside webhook/)
FROM scratch
COPY multisync-webhook /multisync-webhook
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/multisync-webhook"]
