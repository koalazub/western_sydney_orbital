default:
    @npm --prefix worker run

setup:
    npm ci --prefix worker

build:
    npm --prefix worker run build

local:
    npm --prefix worker run local

format:
    npm --prefix worker run format

check:
    npm --prefix worker run check

test:
    npm --prefix worker test

transit-stress:
    npm --prefix worker run test:transit

verify:
    npm --prefix worker run verify

deploy:
    npm --prefix worker run deploy
