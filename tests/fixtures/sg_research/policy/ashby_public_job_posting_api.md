---
updatedAt: 2026-05-26T18:00:40.000Z
agentTools:
  projectIndex: https://developers.ashbyhq.com/llms.txt
---

# Ashby Job Postings API

## Introduction

This API allows you to get data for all currently published Job Postings for your organization. If you host your own careers page, you can use this data to populate it. Optionally, you can add `includeCompensation=true` as a query parameter to include compensation data in the response.

## Example

**Request:**

```
curl https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}?includeCompensation=true
```

**Response:**

```javascript
{
  "apiVersion": "1",
  "jobs": [
    {
      "title": "Product Manager",
      "location": "Houston, TX",
      "secondaryLocations": [
        {
          "location": "San Francisco"
          "address": {
            "addressLocality": "San Francisco",
            "addressRegion": "California",
            "addressCountry": "USA"
          }
        }
      ]
      "department": "Product",
      "team": "Growth",
      "isListed": true,
      "isRemote": true,
      "workplaceType": "Remote",
      "descriptionHtml": "<p>Join our team</p>",
      "descriptionPlain": "Join our team",
      "publishedAt": "2021-04-30T16:21:55.393+00:00",
      "employmentType": "FullTime",
      "address": {
        "postalAddress": {
          "addressLocality": "Houston",
          "addressRegion": "Texas",
          "addressCountry": "USA"
        }
      },
      "jobUrl": "https://jobs.ashbyhq.com/example_job",
      "applyUrl": "https://jobs.ashbyhq.com/example/apply"
    },
  ],
  "compensation": {
    "compensationTierSummary": "$81K – $87K • 0.5% – 1.75% • Offers Bonus",
    "scrapeableCompensationSalarySummary": "$81K - $87K",
    "compensationTiers": [
      {
        "id": "cedc8928-e850-47e8-a510-e412f61d068a",
        "tierSummary": "Estimated based on experience $81K – $87K • 0.5% – 1.75% • Offers Bonus",
        "title": "Zone A",
        "additionalInformation": null,
        "components": [
          {
            "id": "5d131ad4-6b3a-4f01-938e-511c60bd0398",
            "summary": "0.5% – 1.75%",
            "compensationType": "EquityPercentage",
            "interval": "NONE",
            "currencyCode": null,
            "minValue": 0.5,
            "maxValue": 1.75
          },
          {
            "id": "5c8de1b0-52d6-41d7-8ae3-671ee74c745a",
            "summary": "Estimated based on experience $81K – $87K",
            "compensationType": "Salary",
            "interval": "1 YEAR",
            "currencyCode": "USD",
            "minValue": 81000,
            "maxValue": 87000
          },
          {
            "id": "5f881813-f164-4b91-b652-fd713627e810",
            "summary": "Offers Bonus",
            "compensationType": "Bonus",
            "interval": "1 YEAR",
            "currencyCode": "USD",
            "minValue": null,
            "maxValue": null
          }
        ]
      }
    ],
    "summaryComponents": [
      {
        "compensationType": "EquityPercentage",
        "interval": "NONE",
        "currencyCode": null,
        "minValue": 0.5,
        "maxValue": 1.75
      },
      {
        "compensationType": "Salary",
        "interval": "1 YEAR",
        "currencyCode": "USD",
        "minValue": 81000,
        "maxValue": 87000
      },
      {
        "compensationType": "Bonus",
        "interval": "1 YEAR",
        "currencyCode": "USD",
        "minValue": null,
        "maxValue": null
      }
    ]
  }
}
```

## How to find your jobs page name

Your request must include your organization's Ashby jobs page name in the URL path. To find this name, you can go to your Ashby hosted job board. The final part of the URL path will be the jobs page name. Eg, `https://jobs.ashbyhq.com/Ashby` - "Ashby" is the jobs page name. To find your Ashby hosted job board, in the app head to 'Admin', then to Theme (Job Board). From there, click on the 'Job Board' button in the top-right corner to be taken to your job board.

![](https://slabstatic.com/prod/uploads/l73blu9g/posts/images/4M5qnho-Pe-ybeRUqGWFlO-5.png)

## API

**Request URL**

```
GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}?includeCompensation={true/false}
```

If the request is successful, the response body will be JSON with the following fields. Note that where some piece of this data is missing in Ashby, it will also be missing in this response (for example, if the job's location does not have a country, the country field will be empty).

| **Field**                                          | **Data type** | **Description**                                                                                                                                       |
| -------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apiVersion`                                       | string        | which version of the api generated this response (currently "1").                                                                                     |
| `jobs`                                             | list          | List of jobs                                                                                                                                          |
| `jobs[].title`                                     | string        | Job's title                                                                                                                                           |
| `jobs[].location`                                  | string        | Job's location                                                                                                                                        |
| `jobs[].secondaryLocations`                        | list          | Job's secondary locations                                                                                                                             |
| `jobs[].secondaryLocations[].location`             | string        | Job's secondary location                                                                                                                              |
| `jobs[].secondaryLocations[].address`              | object        |                                                                                                                                                       |
| `secondaryLocations[].address.addressLocality`     | string        | City associated with the job's secondary location                                                                                                     |
| `secondaryLocations[].address.addressRegion`       | string        | State/province associated with the job's secondary location                                                                                           |
| `secondaryLocations[].address.addressCountry`      | string        | Country associated with the job's secondary location                                                                                                  |
| `jobs[].department`                                | string        | Job's department                                                                                                                                      |
| `jobs[].team`                                      | string        | Job's immediate team                                                                                                                                  |
| `jobs[].isRemote`                                  | boolean       | Whether the job is remote                                                                                                                             |
| `jobs[].workplaceType`                             | string        | An enum representing the workplace type, which will be one of the following values: "OnSite", "Remote", or "Hybrid".                                  |
| `jobs[].descriptionHtml`                           | string        | Description of the job formatted as html                                                                                                              |
| `jobs[].descriptionPlain`                          | string        | Description of the job formatted as plain text                                                                                                        |
| `jobs[].publishedAt`                               | string        | ISO DateTime when the job was last published                                                                                                          |
| `jobs[].employmentType`                            | string        | An enum representing the type of employment, which will be one of the following values: "FullTime", "PartTime", "Intern", "Contract", or "Temporary". |
| `jobs[].address`                                   | object        |                                                                                                                                                       |
| `jobs[].address.postalAddress`                     | object        |                                                                                                                                                       |
| `postalAddress.addressLocality`                    | string        | City associated with the job's location                                                                                                               |
| `postalAddress.addressRegion`                      | string        | State/province associated with the job's location                                                                                                     |
| `postalAddress.addressCountry`                     | string        |                                                                                                                                                       |
| `jobs[].jobUrl`                                    | string        | URL of the Ashby hosted page for this job                                                                                                             |
| `jobs[].applyUrl`                                  | string        | URL for the Ashby page where candidates can apply for this job                                                                                        |
| `jobs[].isListed`                                  | boolean       | Should this job be listed in the list of postings on your job board. If false, this listing should only be available via direct link.                 |
| `jobs[].compensation`                              | object        | Job's compensation information. Included if includeCompensation query parameter is true                                                               |
| `compensation.compensationTierSummary`             | string        | Summary of compensation tiers                                                                                                                         |
| `compensation.scrapeableCompensationSalarySummary` | string        | Scrapeable salary compensation summary                                                                                                                |
| `compensation.compensationTiers[]`                 | object        | Job's compensation tiers                                                                                                                              |
| `compensation.summaryComponents[]`                 | object        | Job 's summary components                                                                                                                             |

If there is additional data you'd like to have included in this response but currently is not, please reach out to us and let us know.