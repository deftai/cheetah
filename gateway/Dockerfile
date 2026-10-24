FROM golang:1.26-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /cheetah-gateway .

FROM alpine:3.24
RUN apk add --no-cache ca-certificates && adduser -D -u 1001 appuser
COPY --from=builder /cheetah-gateway /usr/local/bin/cheetah-gateway
USER appuser
EXPOSE 3000
CMD ["cheetah-gateway"]
